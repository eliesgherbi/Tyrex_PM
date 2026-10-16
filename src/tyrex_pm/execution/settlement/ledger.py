"""Durable cross-window settlement ledger (event-sourced, append-only, fsynced).

One *episode* is one intended position lifecycle in one window; it may need several
entry *attempts* (retry) and a window may hold several episodes (re-entry after a
complete sale). The ledger is the authority for what this account has at stake across
windows and process restarts:

    RESERVED ─► NO_DISPATCH | NO_FILL            (reservation released)
       ▲    │       └──ATTEMPT_REARMED──► RESERVED  (retry: fresh reservation, attempt+1)
       │    └─► DISPATCH_UNKNOWN                 (reservation kept until reconciled)
       │    └─► FILLED ─► HOLD_REQUESTED ─► HOLD_COMMITTED ─► PAYOUT_PROVEN
       │          │  ▲                                  └─► RESOLUTION_UNCERTAIN
       │          │  └─ EXIT_FILL_CONFIRMED (partial early sale; managed exits)
       │          └─► SOLD                         (complete early sale; terminal)
       │       PAYOUT_PROVEN ─► REDEMPTION_PENDING_OPERATOR ─► CASH_CONFIRMED ─► COMPLETED
       │       PAYOUT_PROVEN (zero payout) ─► COMPLETED
    any ─► MANUAL_INTERVENTION

Money is never counted twice. An episode contributes to exactly one bucket:

* ``reserved`` — RESERVED / DISPATCH_UNKNOWN: cash reserved for the worst-case debit
  plus attributable fees of an order that could still fill;
* ``spent``    — FILLED .. PAYOUT_PROVEN / REDEMPTION_PENDING_OPERATOR: cash already
  spent (remaining cost basis); the reservation is released by the fill, never
  subtracted again; partial sale proceeds are cash again (not spent);
* ``receivable`` — PAYOUT_PROVEN with positive payout, until cash is confirmed;
* ``realized`` — COMPLETED / SOLD: credited cash plus sale proceeds minus total cost.

Schema 2 (this file) reads schema-1 journals unchanged: the first attempt of the first
episode of a window keeps the schema-1 episode id and dedupe keys. Each record has a
dedupe key; replaying a record with the same key is ignored, a conflicting record for
the same key is refused. A torn last line (crash mid-write) is skipped on recovery; any
other unreadable line is a hard error.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
OPERATOR_SCOPE = "__operator__"


class EpisodeState(str, Enum):
    RESERVED = "RESERVED"
    NO_DISPATCH = "NO_DISPATCH"
    NO_FILL = "NO_FILL"
    DISPATCH_UNKNOWN = "DISPATCH_UNKNOWN"
    FILLED = "FILLED"
    HOLD_REQUESTED = "HOLD_REQUESTED"
    HOLD_COMMITTED = "HOLD_COMMITTED"
    RESOLUTION_UNCERTAIN = "RESOLUTION_UNCERTAIN"
    PAYOUT_PROVEN = "PAYOUT_PROVEN"
    REDEMPTION_PENDING_OPERATOR = "REDEMPTION_PENDING_OPERATOR"
    CASH_CONFIRMED = "CASH_CONFIRMED"
    COMPLETED = "COMPLETED"
    SOLD = "SOLD"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"


S = EpisodeState
TRANSITIONS: dict[str, tuple[frozenset[EpisodeState | None], EpisodeState]] = {
    "EPISODE_RESERVED": (frozenset({None}), S.RESERVED),
    "ATTEMPT_REARMED": (frozenset({S.NO_DISPATCH, S.NO_FILL}), S.RESERVED),
    "DISPATCH_NOT_SENT": (frozenset({S.RESERVED}), S.NO_DISPATCH),
    "DISPATCH_NO_FILL": (frozenset({S.RESERVED, S.DISPATCH_UNKNOWN}), S.NO_FILL),
    "DISPATCH_UNKNOWN": (frozenset({S.RESERVED}), S.DISPATCH_UNKNOWN),
    "FILL_CONFIRMED": (frozenset({S.RESERVED, S.DISPATCH_UNKNOWN}), S.FILLED),
    "EXIT_FILL_CONFIRMED": (frozenset({S.FILLED}), S.FILLED),
    "EPISODE_SOLD": (frozenset({S.FILLED}), S.SOLD),
    "HOLD_REQUESTED": (frozenset({S.FILLED}), S.HOLD_REQUESTED),
    "HOLD_COMMITTED": (frozenset({S.HOLD_REQUESTED}), S.HOLD_COMMITTED),
    "RESOLUTION_UNCERTAIN": (
        frozenset({S.HOLD_COMMITTED, S.RESOLUTION_UNCERTAIN}),
        S.RESOLUTION_UNCERTAIN,
    ),
    "PAYOUT_PROVEN": (frozenset({S.HOLD_COMMITTED, S.RESOLUTION_UNCERTAIN}), S.PAYOUT_PROVEN),
    "REDEMPTION_REQUESTED": (frozenset({S.PAYOUT_PROVEN}), S.REDEMPTION_PENDING_OPERATOR),
    "CASH_CONFIRMED": (frozenset({S.REDEMPTION_PENDING_OPERATOR}), S.CASH_CONFIRMED),
    "EPISODE_COMPLETED": (frozenset({S.CASH_CONFIRMED, S.PAYOUT_PROVEN}), S.COMPLETED),
    "MANUAL_INTERVENTION": (frozenset(set(S)) - {S.COMPLETED, S.SOLD}, S.MANUAL_INTERVENTION),
}
# Run-level operator records (no episode): acknowledgement of a consecutive-loss pause.
OPERATOR_EVENTS = frozenset({"LOSS_PAUSE_ACKNOWLEDGED"})

RESERVED_STATES = {S.RESERVED, S.DISPATCH_UNKNOWN}
SPENT_STATES = {
    S.FILLED,
    S.HOLD_REQUESTED,
    S.HOLD_COMMITTED,
    S.RESOLUTION_UNCERTAIN,
    S.PAYOUT_PROVEN,
    S.REDEMPTION_PENDING_OPERATOR,
}
UNRESOLVED_STATES = SPENT_STATES  # exposure not yet converted into confirmed cash
TERMINAL_STATES = {S.NO_DISPATCH, S.NO_FILL, S.COMPLETED, S.SOLD}
# Episodes that still hold or may hold inventory or reservations in their window.
OPEN_STATES = RESERVED_STATES | {S.FILLED, S.HOLD_REQUESTED, S.MANUAL_INTERVENTION}


class LedgerError(RuntimeError):
    pass


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass
class Episode:
    episode_id: str
    window_id: str
    market_id: str
    condition_id: str
    token_id: str
    outcome_index: int
    state: EpisodeState | None = None
    episode_seq: int = 1
    attempts: int = 1
    posts: int = 0
    prepare_failures: int = 0
    reserved_debit: Decimal = Decimal(0)
    reserved_fees: Decimal = Decimal(0)
    net_shares: Decimal = Decimal(0)
    # Remaining cost basis (reduced proportionally by early sales) and the original.
    cost_basis: Decimal = Decimal(0)
    total_cost: Decimal = Decimal(0)
    sold_shares: Decimal = Decimal(0)
    sale_proceeds: Decimal = Decimal(0)
    released_cost: Decimal = Decimal(0)
    payout_per_share: Decimal | None = None
    receivable: Decimal = Decimal(0)
    credited: Decimal = Decimal(0)
    opened_at: str | None = None
    completed_at: str | None = None
    evidence: list[dict] = field(default_factory=list)
    history: list[str] = field(default_factory=list)

    @property
    def realized_pnl(self) -> Decimal | None:
        if self.state is S.COMPLETED:
            return self.credited + self.sale_proceeds - self.total_cost
        if self.state is S.SOLD:
            return self.sale_proceeds - self.total_cost
        return None

    @property
    def worst_case_open_loss(self) -> Decimal:
        """Loss still possible on this episode (spent cost basis or reserved debit)."""
        if self.state in RESERVED_STATES:
            return self.reserved_debit + self.reserved_fees
        if self.state in SPENT_STATES:
            # Loss already locked in by partial sales is part of the open episode.
            locked = max(Decimal(0), self.released_cost - self.sale_proceeds)
            if self.payout_per_share is not None:
                return locked + max(
                    Decimal(0), self.cost_basis - self.net_shares * self.payout_per_share
                )
            return locked + self.cost_basis
        if self.state is S.MANUAL_INTERVENTION:
            # Unknown outcome: everything that might have been committed is at risk.
            return self.reserved_debit + self.reserved_fees + self.cost_basis
        return Decimal(0)


@dataclass(frozen=True)
class LedgerTotals:
    episodes: int
    attempted_episodes: int
    reserved_cash: Decimal
    spent_unresolved: Decimal
    receivable: Decimal
    realized_pnl: Decimal
    realized_losses: Decimal
    unresolved_count: int
    # Pending (could still fill) plus unresolved holdings.
    open_count: int
    open_worst_case_loss: Decimal
    manual_count: int
    unredeemed_count: int = 0
    unredeemed_cost: Decimal = Decimal(0)


@dataclass(frozen=True)
class WindowUsage:
    """Durable per-window counters (restart-proof retry/re-entry budgets)."""

    episodes: int
    filled_episodes: int
    attempts: int
    posts: int
    prepare_failures: int
    open_episode: str | None
    latest_episode: str | None


class SettlementLedger:
    def __init__(self, path: Path, *, fsync: bool = True) -> None:
        self.path = path
        self.fsync = fsync
        self.episodes: dict[str, Episode] = {}
        self.operator_records: list[dict] = []
        self._keys: dict[str, dict] = {}
        self._seq = 0
        self.torn_tail_skipped = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._recover()

    # -- persistence -----------------------------------------------------------------

    def _recover(self) -> None:
        if not self.path.exists():
            return
        lines = self.path.read_bytes().split(b"\n")
        for index, raw in enumerate(lines):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                if index == len(lines) - 1:
                    self.torn_tail_skipped = True  # crash during the last append
                    continue
                raise LedgerError(f"unreadable settlement ledger line {index + 1}") from None
            self._apply(record, replay=True)

    def _append(self, record: dict) -> None:
        line = (json.dumps(record, sort_keys=True, default=str) + "\n").encode("utf-8")
        with self.path.open("ab") as handle:
            handle.write(line)
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())

    # -- events ----------------------------------------------------------------------

    def record(
        self, event: str, episode_id: str, *, key: str | None = None, **payload: Any
    ) -> Episode | None:
        """Durably append one event (before its effect is relied upon) and apply it."""
        dedupe = key or f"{event}:{episode_id}"
        record = {
            "event": event,
            "episode_id": episode_id,
            "key": dedupe,
            "payload": payload,
        }
        previous = self._keys.get(dedupe)
        if previous is not None:
            if previous["event"] != event or previous["payload"] != json.loads(
                json.dumps(payload, sort_keys=True, default=str)
            ):
                raise LedgerError(f"conflicting record for key {dedupe}")
            return self.episodes.get(episode_id)
        self._check(event, episode_id, payload)
        self._seq += 1
        record["seq"] = self._seq
        record["schema"] = SCHEMA_VERSION
        record["recorded_at"] = datetime.now(timezone.utc).isoformat()
        self._append(record)
        self._apply(record, replay=False)
        return self.episodes.get(episode_id)

    def _check(self, event: str, episode_id: str, payload: dict) -> None:
        if event in OPERATOR_EVENTS:
            if episode_id != OPERATOR_SCOPE:
                raise LedgerError(f"{event} is a run-level operator record")
            return
        if event not in TRANSITIONS:
            raise LedgerError(f"unknown settlement event {event}")
        allowed, _ = TRANSITIONS[event]
        current = self.episodes.get(episode_id)
        state = None if current is None else current.state
        if state not in allowed:
            raise LedgerError(f"{event} not allowed from {state} for {episode_id}")
        if event == "EPISODE_RESERVED":
            for field_name in ("window_id", "market_id", "condition_id", "token_id"):
                if not payload.get(field_name):
                    raise LedgerError(f"EPISODE_RESERVED requires {field_name}")
            window = [e for e in self.episodes.values() if e.window_id == payload["window_id"]]
            cap = int(payload.get("window_episode_cap", 1))
            if len(window) + 1 > cap:
                raise LedgerError(f"window {payload['window_id']} already has an episode")
            if any(e.state in OPEN_STATES for e in window):
                raise LedgerError(f"window {payload['window_id']} has an open episode")
        if event == "ATTEMPT_REARMED":
            assert current is not None
            if int(payload.get("attempt", 0)) != current.attempts + 1:
                raise LedgerError("ATTEMPT_REARMED must name the next attempt number")
        if event == "EXIT_FILL_CONFIRMED":
            assert current is not None
            if _dec(payload["sold_shares"]) <= 0 or _dec(payload["sold_shares"]) > (
                current.net_shares
            ):
                raise LedgerError("early sale exceeds the episode's confirmed shares")

    def _apply(self, record: dict, *, replay: bool) -> None:
        event, episode_id, payload = record["event"], record["episode_id"], record["payload"]
        self._keys[record["key"]] = {
            "event": event,
            "payload": json.loads(json.dumps(payload, sort_keys=True, default=str)),
        }
        self._seq = max(self._seq, int(record.get("seq") or 0))
        at = record.get("recorded_at")
        if event in OPERATOR_EVENTS:
            self.operator_records.append({"event": event, "at": at, **payload})
            return
        if event == "EPISODE_RESERVED":
            self.episodes[episode_id] = Episode(
                episode_id=episode_id,
                window_id=payload["window_id"],
                market_id=payload["market_id"],
                condition_id=payload["condition_id"],
                token_id=payload["token_id"],
                outcome_index=int(payload.get("outcome_index", -1)),
                episode_seq=int(payload.get("episode_seq", 1)),
                reserved_debit=_dec(payload.get("max_debit", 0)),
                reserved_fees=_dec(payload.get("max_fees", 0)),
                opened_at=at,
            )
        ep = self.episodes[episode_id]
        _, target = TRANSITIONS[event]
        if payload.get("posted"):
            ep.posts += 1
        if payload.get("prepare_failed"):
            ep.prepare_failures += 1
        if event == "ATTEMPT_REARMED":
            ep.attempts = int(payload["attempt"])
            ep.reserved_debit = _dec(payload.get("max_debit", 0))
            ep.reserved_fees = _dec(payload.get("max_fees", 0))
        elif event in ("DISPATCH_NOT_SENT", "DISPATCH_NO_FILL"):
            ep.reserved_debit = ep.reserved_fees = Decimal(0)
        elif event == "FILL_CONFIRMED":
            # The reservation becomes spent capital exactly once.
            ep.net_shares = _dec(payload["net_shares"])
            ep.cost_basis = ep.total_cost = _dec(payload["cost_basis"])
            ep.reserved_debit = ep.reserved_fees = Decimal(0)
        elif event == "EXIT_FILL_CONFIRMED":
            sold = _dec(payload["sold_shares"])
            released = ep.cost_basis * sold / ep.net_shares
            ep.net_shares -= sold
            ep.cost_basis -= released
            ep.released_cost += released
            ep.sold_shares += sold
            ep.sale_proceeds += _dec(payload["proceeds"])
        elif event == "EPISODE_SOLD":
            # Any residual below the sell increment is classified dust (no cost basis).
            ep.released_cost += ep.cost_basis
            ep.cost_basis = Decimal(0)
            ep.net_shares = Decimal(0)
        elif event == "PAYOUT_PROVEN":
            ep.payout_per_share = _dec(payload["payout_per_share"])
            ep.receivable = ep.net_shares * ep.payout_per_share
        elif event == "CASH_CONFIRMED":
            ep.credited = _dec(payload["credited_amount"])
            ep.receivable = Decimal(0)
        elif event == "EPISODE_COMPLETED" and ep.state is S.PAYOUT_PROVEN:
            if ep.receivable != 0:
                raise LedgerError("only a zero payout completes without confirmed cash")
        if payload.get("evidence"):
            ep.evidence.append({"event": event, **payload["evidence"]})
        ep.state = target
        if target in TERMINAL_STATES:
            ep.completed_at = at
        ep.history.append(event)

    # -- identities ------------------------------------------------------------------

    @staticmethod
    def attempt_key(event: str, episode_id: str, attempt: int) -> str:
        """Dedupe key of an attempt-scoped event; attempt 1 keeps the schema-1 key."""
        return f"{event}:{episode_id}" if attempt <= 1 else f"{event}:{episode_id}:a{attempt}"

    # -- views -----------------------------------------------------------------------

    def by_window(self, window_id: str) -> Episode | None:
        """The window's most recent episode (schema 1: its only episode)."""
        found = [e for e in self.episodes.values() if e.window_id == window_id]
        return max(found, key=lambda e: e.episode_seq) if found else None

    def window_episodes(self, window_id: str) -> list[Episode]:
        return sorted(
            (e for e in self.episodes.values() if e.window_id == window_id),
            key=lambda e: e.episode_seq,
        )

    def window_usage(self, window_id: str) -> WindowUsage:
        eps = self.window_episodes(window_id)
        open_ep = next((e.episode_id for e in eps if e.state in OPEN_STATES), None)
        return WindowUsage(
            episodes=len(eps),
            filled_episodes=sum(1 for e in eps if e.total_cost > 0 or e.net_shares > 0),
            attempts=sum(e.attempts for e in eps),
            posts=sum(e.posts for e in eps),
            prepare_failures=sum(e.prepare_failures for e in eps),
            open_episode=open_ep,
            latest_episode=None if not eps else eps[-1].episode_id,
        )

    def unresolved(self) -> list[Episode]:
        return [e for e in self.episodes.values() if e.state in UNRESOLVED_STATES]

    def needs_reconciliation(self, *, exclude_window: str | None = None) -> list[Episode]:
        return [
            e
            for e in self.episodes.values()
            if (e.state in RESERVED_STATES or e.state in (S.FILLED, S.HOLD_REQUESTED))
            and e.window_id != exclude_window
        ]

    def realized_losses_on(self, day: date) -> Decimal:
        """Realized losses of episodes completed on ``day`` (UTC)."""
        total = Decimal(0)
        for e in self.episodes.values():
            pnl = e.realized_pnl
            if pnl is None or pnl >= 0 or not e.completed_at:
                continue
            if datetime.fromisoformat(e.completed_at).astimezone(timezone.utc).date() == day:
                total += -pnl
        return total

    def consecutive_losses(self) -> int:
        """Trailing losing completions since the last operator acknowledgement.

        Ordered by completion time. Unresolved episodes count neither as wins nor
        losses until they complete (a delayed resolution is inserted at its completion).
        """
        acked = max((r["at"] for r in self.operator_records
                     if r["event"] == "LOSS_PAUSE_ACKNOWLEDGED" and r.get("at")), default=None)
        done = sorted(
            (e for e in self.episodes.values()
             if e.realized_pnl is not None and e.completed_at
             and (acked is None or e.completed_at > acked)),
            key=lambda e: e.completed_at or "",
        )
        count = 0
        for e in reversed(done):
            pnl = e.realized_pnl
            if pnl is None or pnl >= 0:
                break
            count += 1
        return count

    def totals(self) -> LedgerTotals:
        eps = list(self.episodes.values())
        realized = [e.realized_pnl for e in eps if e.realized_pnl is not None]
        unredeemed = [e for e in eps if e.state is S.REDEMPTION_PENDING_OPERATOR]
        return LedgerTotals(
            episodes=len(eps),
            attempted_episodes=sum(1 for e in eps if e.state is not S.NO_DISPATCH),
            reserved_cash=sum(
                (e.reserved_debit + e.reserved_fees for e in eps if e.state in RESERVED_STATES),
                Decimal(0),
            ),
            spent_unresolved=sum(
                (e.cost_basis for e in eps if e.state in SPENT_STATES), Decimal(0)
            ),
            receivable=sum((e.receivable for e in eps), Decimal(0)),
            realized_pnl=sum(realized, Decimal(0)),
            realized_losses=sum((-r for r in realized if r < 0), Decimal(0)),
            unresolved_count=sum(1 for e in eps if e.state in UNRESOLVED_STATES),
            open_count=sum(
                1
                for e in eps
                if e.state in UNRESOLVED_STATES
                or e.state in RESERVED_STATES
                or e.state is S.MANUAL_INTERVENTION
            ),
            open_worst_case_loss=sum((e.worst_case_open_loss for e in eps), Decimal(0)),
            manual_count=sum(1 for e in eps if e.state is S.MANUAL_INTERVENTION),
            unredeemed_count=len(unredeemed),
            unredeemed_cost=sum((e.receivable for e in unredeemed), Decimal(0)),
        )
