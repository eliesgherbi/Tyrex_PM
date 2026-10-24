"""Settlement-ledger hooks for the trading runtime (``lifecycle.mode: hold_to_resolution``
and ``managed_exit``).

The trading runtime calls these at fixed points; legacy profiles never construct them.

* ``before_entry`` — immediately before an entry reaches the dispatch path: verified
  current constraints (fees from provider metadata matching the strategy's curve, tick,
  minimum size), then the trial/run budget over the durable settlement ledger (pending,
  uncertain, unresolved and realized, plus the proposed order) and the durable window
  budget (episodes, attempts, POSTs), then a durable ``EPISODE_RESERVED`` record — or,
  for a retry inside the same episode, ``ATTEMPT_REARMED``. Any failure blocks the
  entry with stable reasons.
* ``after_entry`` — records the authoritative dispatch outcome of that attempt: filled
  (reservation becomes spent capital once), no fill / no dispatch (released) or unknown
  (kept). Whether the attempt reached a POST or failed in preparation is recorded too.
* ``after_exit`` — records a confirmed early sale (managed exits): partial sales reduce
  the episode's shares and cost basis; a complete sale closes the episode (``SOLD``).
* ``commit_hold`` — requires confirmed inventory, no unresolved mutation and no live
  resting order (cancelled first by the caller, proof required), then
  ``HOLD_REQUESTED`` and ``HOLD_COMMITTED``. Only a committed holding is exempt from
  mandatory flatten, the manual-deadline stop, protection and the shutdown sale.
* ``committed_holds`` — (condition, token) pairs that startup reconciliation must treat
  as intentional settlement exposure, never as positions to close.

Episode identity: the first episode of a window keeps the schema-1 id
``run_name:window_id``; later episodes are ``run_name:window_id:e<seq>``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from tyrex_pm.execution.session_state import ExecutionPhase
from tyrex_pm.execution.settlement.constraints import (
    FeeScheduleSource,
    gamma_fee_schedule,
    verify_constraints,
)
from tyrex_pm.execution.settlement.governor import WindowBudget, check_entry
from tyrex_pm.execution.settlement.ledger import (
    EpisodeState,
    SettlementLedger,
)

S = EpisodeState
def startup_exposure_decision(
    *,
    has_exposure: bool,
    episode_state: str | None,
    stored_policy: str | None,
    loaded_policy: str | None,
) -> str:
    """What startup may do with confirmed exposure from an earlier process.

    ``continue`` — nothing to recover. ``stop`` — not a settlement-owned open
    position; the documented safety stop still applies. ``commit_hold`` and
    ``resume_managed`` continue the policy recorded when the episode was reserved.
    ``track_hold`` — the episode is already committed; keep it and do not stop.
    ``block`` — the original policy is missing or disagrees with the loaded
    configuration, so startup must not commit, sell, or flatten it.
    """
    if not has_exposure:
        return "continue"
    if episode_state in (
        "HOLD_COMMITTED",
        "RESOLUTION_UNCERTAIN",
        "PAYOUT_PROVEN",
        "REDEMPTION_PENDING_OPERATOR",
        "CASH_CONFIRMED",
        "COMPLETED",
    ):
        return "track_hold"
    if episode_state not in ("FILLED", "HOLD_REQUESTED"):
        return "stop"
    if (
        stored_policy not in ("hold_to_resolution", "managed_exit")
        or stored_policy != loaded_policy
    ):
        return "block"
    if stored_policy == "hold_to_resolution":
        return "commit_hold"
    return "resume_managed"


COMMITTED_STATES = frozenset(
    {
        S.HOLD_COMMITTED,
        S.RESOLUTION_UNCERTAIN,
        S.PAYOUT_PROVEN,
        S.REDEMPTION_PENDING_OPERATOR,
        S.CASH_CONFIRMED,
        S.COMPLETED,
    }
)


@dataclass
class EntryCheck:
    allowed: bool
    reasons: tuple[str, ...]
    evidence: dict[str, Any] = field(default_factory=dict)
    episode_id: str | None = None
    attempt: int = 1


def window_budget_for(strategy: Any) -> WindowBudget:
    """Durable window ceilings from a strategy's retry/re-entry policy (default: one)."""
    retry = getattr(strategy, "retry", None)
    reentry = getattr(strategy, "reentry", None)
    episodes = int(getattr(reentry, "max_trades_per_window", 1) or 1)
    if retry is None or not getattr(retry, "enabled", False):
        return WindowBudget(
            max_episodes=episodes,
            max_attempts=episodes,
            max_posts=episodes,
        )
    return WindowBudget(
        max_episodes=episodes,
        max_attempts=int(retry.max_attempts_per_window),
        max_posts=int(retry.max_posts_per_window),
        max_attempts_per_episode=int(retry.max_attempts_per_episode),
        max_posts_per_episode=int(retry.max_posts_per_episode),
        max_prepare_failures_per_episode=int(retry.max_prepare_failures_per_episode),
    )


@dataclass
class SettlementHooks:
    config: Any  # TradingRunConfig with a settlement block
    ledger: SettlementLedger
    fee_schedule_source: FeeScheduleSource = gamma_fee_schedule

    @classmethod
    def from_config(cls, config: Any, **kwargs: Any) -> SettlementHooks | None:
        settlement = getattr(config, "settlement", None)
        if getattr(config, "lifecycle_mode", None) not in (
            "hold_to_resolution",
            "managed_exit",
        ) or settlement is None:
            return None
        writer = kwargs.pop("ledger_writer", None)
        ledger = writer.ledger() if writer is not None else SettlementLedger(settlement.ledger_path)
        return cls(config=config, ledger=ledger, **kwargs)

    def episode_id(self, window_id: str, seq: int = 1) -> str:
        base = f"{self.config.run_name}:{window_id}"
        return base if seq <= 1 else f"{base}:e{seq}"

    def current_episode(self, window_id: str | None):
        if window_id is None:
            return None
        return self.ledger.by_window(window_id)

    @property
    def window_budget(self) -> WindowBudget:
        return window_budget_for(getattr(self.config, "strategy", None))

    # -- startup ---------------------------------------------------------------------

    def committed_holds(self, *, include_settled: bool = False) -> frozenset[tuple[str, str]]:
        """(condition, token) pairs the ledger owns as settlement exposure.

        ``include_settled`` (continuous runs) also returns holdings whose episode already
        completed (payout proven and redeemed, or zero payout): the ledger proves how the
        tokens left (or why worthless ones remain), so they are never positions to close.
        """
        states = {
            S.FILLED,
            S.HOLD_REQUESTED,
            S.HOLD_COMMITTED,
            S.RESOLUTION_UNCERTAIN,
            S.PAYOUT_PROVEN,
            S.REDEMPTION_PENDING_OPERATOR,
        }
        if include_settled:
            states |= {S.CASH_CONFIRMED, S.COMPLETED}
        return frozenset(
            (ep.condition_id, ep.token_id)
            for ep in self.ledger.episodes.values()
            if ep.state in states
        )

    def startup_blockers(self, *, window_id: str | None = None) -> tuple[str, ...]:
        """Episodes left between reservation and a known outcome block entries.

        ``window_id`` excludes the window currently being traded: its open episode is
        owned by this process and is handled by its own lifecycle.
        """
        stuck = [ep.episode_id for ep in self.ledger.needs_reconciliation(exclude_window=window_id)]
        return ("SETTLEMENT_EPISODE_NEEDS_RECONCILIATION",) if stuck else ()

    # -- entry -----------------------------------------------------------------------

    async def before_entry(
        self,
        *,
        window_id: str,
        window_slug: str,
        market_id: str,
        condition_id: str,
        token_id: str,
        outcome_index: int,
        target_notional: Decimal,
        tick_size: Decimal | None,
        min_order_size: Decimal | None,
        strategy_fee_curve: Any,
        episode_seq: int = 1,
        attempt_seq: int = 1,
        available_collateral: Decimal | None = None,
        collateral_read_started_at: datetime | None = None,
        today: date | None = None,
        lifecycle_policy: str | None = None,
        entry_started_at: str | None = None,
    ) -> EntryCheck:
        reasons: list[str] = list(self.startup_blockers())
        try:
            schedule = await self.fee_schedule_source(window_slug, condition_id)
        except Exception as exc:  # noqa: BLE001 - unknown fees block, never default
            schedule = None
            reasons.append(f"FEE_SCHEDULE_READ_FAILED:{type(exc).__name__}")
        constraints = verify_constraints(
            fee_schedule=schedule,
            strategy_fee_curve=strategy_fee_curve,
            tick_size=tick_size,
            min_order_size=min_order_size,
            fee_source="gamma_fee_schedule",
        )
        reasons.extend(constraints.reasons)
        cap = self.config.risk.maximum_total_debit
        # The planner spends desired / (1 + fee_reserve_rate): the cap already holds the
        # fee reserve, and taker buy fees are charged in shares, so the worst-case cash
        # debit of this order is the capped desired notional.
        proposed = min(target_notional, cap)
        episode_id = self.episode_id(window_id, episode_seq)
        existing = self.ledger.episodes.get(episode_id)
        rearm = (
            existing is not None
            and attempt_seq > 1
            and existing.state in (S.NO_DISPATCH, S.NO_FILL)
        )
        if existing is not None and not rearm:
            reasons.append(
                "EPISODE_ALREADY_EXISTS_FOR_WINDOW" if attempt_seq <= 1
                else "OPEN_EPISODE_EXISTS_FOR_WINDOW"
            )
        settlement = self.config.settlement
        # A late account read must never restore spent money: subtract fills the
        # ledger recorded after this collateral read started.
        spent_after_read = (
            Decimal(0)
            if available_collateral is None
            else self.ledger.spent_since(collateral_read_started_at)
        )
        if available_collateral is not None:
            available_collateral = available_collateral - spent_after_read
        budget = check_entry(
            self.ledger,
            settlement.trial,
            window_id=window_id,
            proposed_debit=proposed,
            proposed_fees=Decimal(0),
            rearm_episode=episode_id if rearm else None,
            window_budget=self.window_budget,
            run_limits=getattr(settlement, "run_limits", None),
            available_collateral=available_collateral,
            today=today,
        )
        reasons.extend(budget.reasons)
        evidence = {
            "episode_id": episode_id,
            "attempt": attempt_seq,
            "constraints": {
                "ok": constraints.ok,
                "tick_size": str(tick_size),
                "min_order_size": str(min_order_size),
                "fee_curve": None
                if constraints.fee_curve is None
                else [str(constraints.fee_curve.fee_rate), str(constraints.fee_curve.exponent)],
            },
            "available_collateral": {
                "after_fills_since_read": None
                if available_collateral is None
                else str(available_collateral),
                "spent_since_read": str(spent_after_read),
                "collateral_read_started_at": None
                if collateral_read_started_at is None
                else collateral_read_started_at.isoformat(),
            },
            "budget": {
                "loss_budget": str(budget.loss_budget),
                "used_after": str(budget.loss_budget_used_after),
                "realized_losses": str(budget.realized_losses),
                "open_worst_case": str(budget.open_worst_case_loss),
                "proposed": str(budget.proposed_worst_case),
                "unresolved_cost_after": str(budget.unresolved_cost_after),
                "daily_loss_used_after": None
                if budget.daily_loss_used_after is None
                else str(budget.daily_loss_used_after),
            },
        }
        if reasons:
            return EntryCheck(False, tuple(dict.fromkeys(reasons)), evidence, episode_id,
                              attempt_seq)
        if rearm:
            assert existing is not None
            self.ledger.record(
                "ATTEMPT_REARMED",
                episode_id,
                key=SettlementLedger.attempt_key("ATTEMPT_REARMED", episode_id, attempt_seq),
                attempt=existing.attempts + 1,
                max_debit=str(proposed),
                max_fees="0",
            )
        else:
            self.ledger.record(
                "EPISODE_RESERVED",
                episode_id,
                window_id=window_id,
                market_id=market_id,
                condition_id=condition_id,
                token_id=token_id,
                outcome_index=outcome_index,
                max_debit=str(proposed),
                max_fees="0",
                **{
                    **({} if not lifecycle_policy else {"lifecycle_policy": lifecycle_policy}),
                    **({} if not entry_started_at else {"entry_started_at": entry_started_at}),
                    **(
                        {}
                        if episode_seq <= 1
                        else {
                            "episode_seq": episode_seq,
                            "window_episode_cap": self.window_budget.max_episodes,
                        }
                    ),
                },
            )
        return EntryCheck(True, (), evidence, episode_id, attempt_seq)

    def after_entry(
        self,
        *,
        window_id: str,
        state: Any,
        cost_basis: Decimal,
        episode_seq: int = 1,
        posted: bool | None = None,
        prepare_failed: bool = False,
        entry_started_at: str | None = None,
        retry_clock: str | None = None,
        retry_not_before: str | None = None,
        entry_side: str | None = None,
        consecutive_service_failures: int | None = None,
    ) -> str:
        episode = self.episode_id(window_id, episode_seq)
        ep = self.ledger.episodes.get(episode)
        if ep is None:
            return "NO_EPISODE"
        attempt = ep.attempts
        phase = None if state is None else state.phase
        shares = Decimal(0) if state is None else state.confirmed_position_shares
        extra: dict[str, Any] = {}
        if posted is not None and (posted or prepare_failed or attempt > 1):
            extra = {"posted": posted, "prepare_failed": prepare_failed, "attempt": attempt}
        if retry_clock in ("deadline", "none"):
            extra["retry_clock"] = retry_clock
            if entry_started_at:
                extra["entry_started_at"] = entry_started_at
            if retry_not_before:
                extra["retry_not_before"] = retry_not_before
            if entry_side:
                extra["entry_side"] = entry_side
            if consecutive_service_failures is not None:
                extra["consecutive_service_failures"] = int(consecutive_service_failures)

        def key(event: str) -> str:
            return SettlementLedger.attempt_key(event, episode, attempt)

        if shares > 0:
            self.ledger.record("FILL_CONFIRMED", episode, key=key("FILL_CONFIRMED"),
                               net_shares=str(shares), cost_basis=str(cost_basis), **extra)
            return "FILL_CONFIRMED"
        if phase is ExecutionPhase.COMPLETED_NO_DISPATCH:
            self.ledger.record("DISPATCH_NOT_SENT", episode, key=key("DISPATCH_NOT_SENT"),
                               **extra)
            return "DISPATCH_NOT_SENT"
        if phase is ExecutionPhase.COMPLETED_NO_FILL:
            self.ledger.record("DISPATCH_NO_FILL", episode, key=key("DISPATCH_NO_FILL"),
                               **extra)
            return "DISPATCH_NO_FILL"
        self.ledger.record("DISPATCH_UNKNOWN", episode, key=key("DISPATCH_UNKNOWN"),
                           phase=None if phase is None else phase.value, **extra)
        return "DISPATCH_UNKNOWN"

    def resolve_unknown(
        self, *, window_id: str, state: Any, cost_basis: Decimal, episode_seq: int = 1
    ) -> str | None:
        """Record the authoritative outcome of a DISPATCH_UNKNOWN attempt once known."""
        episode = self.episode_id(window_id, episode_seq)
        ep = self.ledger.episodes.get(episode)
        if ep is None or ep.state is not S.DISPATCH_UNKNOWN or state is None:
            return None
        key = SettlementLedger.attempt_key
        if state.confirmed_position_shares > 0:
            self.ledger.record("FILL_CONFIRMED", episode,
                               key=key("FILL_CONFIRMED", episode, ep.attempts),
                               net_shares=str(state.confirmed_position_shares),
                               cost_basis=str(cost_basis))
            return "FILL_CONFIRMED"
        if state.phase in (ExecutionPhase.COMPLETED_NO_FILL, ExecutionPhase.COMPLETED_NO_DISPATCH):
            self.ledger.record("DISPATCH_NO_FILL", episode,
                               key=key("DISPATCH_NO_FILL", episode, ep.attempts))
            return "DISPATCH_NO_FILL"
        return None

    # -- exits -----------------------------------------------------------------------

    def after_exit(
        self,
        *,
        window_id: str,
        sold_shares: Decimal,
        proceeds: Decimal,
        flat: bool,
        exit_key: str,
        episode_seq: int | None = None,
    ) -> str:
        """Record a confirmed early sale. ``exit_key`` identifies the sale (dedupe)."""
        ep = (
            self.ledger.episodes.get(self.episode_id(window_id, episode_seq))
            if episode_seq is not None
            else self.ledger.by_window(window_id)
        )
        if ep is None or ep.state is not S.FILLED:
            return "NO_OPEN_EPISODE"
        if sold_shares > 0:
            self.ledger.record(
                "EXIT_FILL_CONFIRMED",
                ep.episode_id,
                key=f"EXIT_FILL_CONFIRMED:{ep.episode_id}:{exit_key}",
                sold_shares=str(min(sold_shares, ep.net_shares)),
                proceeds=str(proceeds),
            )
        if flat:
            self.ledger.record("EPISODE_SOLD", ep.episode_id,
                               dust_shares=str(ep.net_shares))
            return "EPISODE_SOLD"
        return "EXIT_FILL_CONFIRMED" if sold_shares > 0 else "NO_SALE"

    # -- hold ------------------------------------------------------------------------

    async def commit_hold(
        self,
        *,
        window_id: str,
        state: Any,
        cancel_conflicting: Callable[[], Awaitable[Any]],
    ) -> tuple[bool, str]:
        ep = self.ledger.by_window(window_id)
        if ep is None:
            return False, "NO_EPISODE"
        episode = ep.episode_id
        if ep.state in COMMITTED_STATES:
            return True, "ALREADY_COMMITTED"
        if ep.state is not S.FILLED or state is None or state.confirmed_position_shares <= 0:
            return False, "NO_CONFIRMED_INVENTORY"
        if state.confirmed_position_shares != ep.net_shares:
            return False, "INVENTORY_DIFFERS_FROM_LEDGER"
        if state.resting_limit_venue_ids or state.working_maker_entry:
            state = await cancel_conflicting()
        if state is None or state.resting_limit_venue_ids or state.working_maker_entry:
            return False, "CONFLICTING_ORDER_NOT_PROVEN_CANCELLED"
        if state.has_unresolved_mutation_attempt:
            return False, "UNRESOLVED_MUTATION"
        quantity = str(state.confirmed_position_shares)
        self.ledger.record("HOLD_REQUESTED", episode, quantity=quantity)
        self.ledger.record("HOLD_COMMITTED", episode, quantity=quantity)
        return True, "HOLD_COMMITTED"

    def is_committed(self, window_id: str | None) -> bool:
        if window_id is None:
            return False
        ep = self.ledger.by_window(window_id)
        # Resolution tracking moves a committed hold on (uncertain, proven, redemption,
        # completed); it never stops being a hold the host must not sell.
        return ep is not None and ep.state in COMMITTED_STATES
