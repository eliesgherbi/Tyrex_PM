"""Auditable repair of a window whose filled position was mis-owned (pre-fix runs).

Applies to the defect fixed alongside this module: a never-sent attempt session claimed
the shares a sibling attempt bought (balance minus a shared baseline), and the hold of a
``hold_to_resolution`` profile was never committed because the account refresh flagged
the run's own position as unknown inventory.

The repair never deletes, rewrites or clears anything. It verifies the evidence (and
fails closed when anything differs from the expected shape), then appends corrections
with fixed keys, so a second run is a no-op:

1. journal, never-sent attempt: a ``ReconciliationObserved`` of 0 shares (source
   ``ownership_correction``) that supersedes the false observation, naming it;
2. ledger, filled episode: ``HOLD_REQUESTED`` + ``HOLD_COMMITTED`` (operator
   correction; the position was never sold, so it was held to resolution);
3. ledger (``--connect-chain``): the established resolution workflow reads the
   finalized on-chain payout (``PAYOUT_PROVEN`` / ``EPISODE_COMPLETED``);
4. journal, filled attempt (after a proven payout): ``MarketSettlementObserved`` citing
   that payout, which moves the shares into settlement holdings (position 0,
   ``SETTLEMENT_ONLY``) as the startup reconciliation does for settled markets.

It never submits, cancels or redeems anything.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from tyrex_pm.execution.evidence import (
    ExecutionRole,
    MarketSettlementObserved,
    ReconciliationObserved,
    TradeStatus,
    new_envelope,
)
from tyrex_pm.execution.settlement.ledger import EpisodeState, SettlementLedger

S = EpisodeState
CORRECTION = "ownership-correction-v1"
REASON = (
    "operator correction: position never sold; hold_to_resolution commit missed because "
    "the run's own fill was flagged as unknown inventory (fixed in code)"
)


class RepairRefused(RuntimeError):
    """The evidence does not match the defect this repair is for; nothing is changed."""


class _NoVenue:
    """The repair must never reach the venue (only the coordinator's shutdown no-ops)."""

    async def stop_user_stream(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"ownership repair never calls the venue ({name})")


@dataclass
class RepairPlan:
    window_id: str
    episode_id: str
    never_sent_session: str
    filled_session: str
    false_observation_ids: list[str]
    shares: Decimal
    cost: Decimal
    condition_id: str
    token_id: str
    checks: list[str] = field(default_factory=list)


def _session_summary(state: Any) -> dict:
    return {
        "phase": state.phase.value,
        "confirmed_position_shares": str(state.confirmed_position_shares),
        "settlement_holding_shares": str(state.settlement_holding_shares),
        "operational_disposition": state.operational_disposition.value,
        "safe_to_open_entry": state.safe_to_open_entry,
        "event_count": state.event_count,
    }


def _episode_summary(ep: Any) -> dict:
    return {
        "state": None if ep.state is None else ep.state.value,
        "net_shares": str(ep.net_shares),
        "cost_basis": str(ep.cost_basis),
        "payout_per_share": None if ep.payout_per_share is None else str(ep.payout_per_share),
        "realized_pnl": None if ep.realized_pnl is None else str(ep.realized_pnl),
        "history": list(ep.history),
    }


def _entry_orders(state: Any) -> list:
    return [o for o in state.orders.values() if o.role is ExecutionRole.ENTRY]


def _confirmed_buys(state: Any) -> list:
    return [t for t in state.trades.values()
            if t.side == "BUY" and t.status is TradeStatus.CONFIRMED]


def plan_repair(window_id: str, ledger: SettlementLedger, states: dict[str, Any],
                journal: Any) -> RepairPlan:
    """Verify the evidence for ``window_id``; raise ``RepairRefused`` if it differs."""
    ep = ledger.by_window(window_id)
    if ep is None:
        raise RepairRefused(f"no ledger episode for {window_id}")
    allowed = {S.FILLED, S.HOLD_REQUESTED, S.HOLD_COMMITTED, S.RESOLUTION_UNCERTAIN,
               S.PAYOUT_PROVEN, S.REDEMPTION_PENDING_OPERATOR, S.CASH_CONFIRMED, S.COMPLETED}
    if ep.state not in allowed:
        raise RepairRefused(f"episode state {ep.state} is not a filled episode")
    if "FILL_CONFIRMED" not in ep.history or ep.sold_shares:
        raise RepairRefused("episode has no confirmed fill, or shares were sold")
    sessions = {sid: st for sid, st in states.items()
                if st.identity is not None and st.identity.window_id == window_id}
    never_sent = [sid for sid, st in sessions.items()
                  if _entry_orders(st)
                  and not any(o.attempt_ids or o.venue_order_id for o in _entry_orders(st))
                  and not any(t.side == "BUY" for t in st.trades.values())]
    filled = [sid for sid, st in sessions.items() if _confirmed_buys(st)]
    if len(never_sent) != 1 or len(filled) != 1 or set(never_sent) & set(filled):
        raise RepairRefused(
            f"expected one never-sent and one filled attempt, got {never_sent} / {filled}")
    a, b = never_sent[0], filled[0]
    state_b = states[b]
    buys = _confirmed_buys(state_b)
    shares = sum((t.shares for t in buys), Decimal(0))
    cost = sum((t.shares * t.price for t in buys), Decimal(0))
    if shares != ep.net_shares:
        raise RepairRefused(f"filled shares {shares} differ from ledger {ep.net_shares}")
    if abs(cost - ep.cost_basis) > Decimal("0.01"):
        raise RepairRefused(f"filled cost {cost} differs from ledger {ep.cost_basis}")
    if any(o.attempt_ids for o in state_b.orders.values() if o.role is ExecutionRole.EXIT):
        raise RepairRefused("an exit order of the filled attempt reached a POST attempt")
    if any(t.side == "SELL" for t in state_b.trades.values()):
        raise RepairRefused("the filled attempt has a SELL trade")
    false_obs = [
        e.event_id for e in journal.load(a)
        if type(e).__name__ == "ReconciliationObserved"
        and Decimal(str(e.confirmed_position_shares)) > 0
    ]
    return RepairPlan(
        window_id=window_id, episode_id=ep.episode_id, never_sent_session=a,
        filled_session=b, false_observation_ids=false_obs, shares=shares, cost=cost,
        condition_id=state_b.identity.market_id, token_id=state_b.identity.token_id,
        checks=[
            f"attempt {a}: entry orders without POST attempt or venue order; no BUY trade",
            f"attempt {b}: {len(buys)} confirmed BUY trade(s), {shares} shares, cost {cost}",
            f"ledger {ep.episode_id}: FILL_CONFIRMED {ep.net_shares} shares, "
            f"cost {ep.cost_basis}; no shares sold",
            f"attempt {b}: no EXIT order reached a POST attempt (never sold)",
            f"false balance attributions on {a}: {false_obs or 'none'}",
        ],
    )


async def run_repair(config: Any, window_id: str, *, apply: bool, connect_chain: bool,
                     dotenv: Any = None) -> dict:
    from tyrex_pm.execution.coordinator import AccountExecutionCoordinator
    from tyrex_pm.execution.process_lock import LedgerWriter, StateDirectoryLock
    from tyrex_pm.persistence.execution_journal import SqliteExecutionJournal

    lock = StateDirectoryLock(config.state_directory)
    lock.acquire()  # refuses while a live run owns the state
    writer = LedgerWriter(config.settlement.ledger_path)
    try:
        writer.acquire()
        ledger = writer.ledger()
        journal = SqliteExecutionJournal(config.state_directory / "execution.sqlite3")

        async def deny(_spec):
            return False, "ownership repair never dispatches"

        coordinator = AccountExecutionCoordinator(journal=journal, gateway=_NoVenue(),
                                                  final_gate=deny)
        for session_id in journal.sessions():
            await coordinator.restore(session_id)
        plan = plan_repair(window_id, ledger, coordinator.states, journal)
        a, b, ep_id = plan.never_sent_session, plan.filled_session, plan.episode_id
        report: dict = {
            "window_id": window_id, "apply": apply, "checks": plan.checks,
            "before": {"never_sent": _session_summary(coordinator.states[a]),
                       "filled": _session_summary(coordinator.states[b]),
                       "episode": _episode_summary(ledger.episodes[ep_id])},
            "actions": [],
        }
        if not apply:
            report["actions"].append("dry run: nothing written (pass --apply)")
            await coordinator.close()
            return report

        # 1. Supersede the false attribution on the never-sent attempt.
        if coordinator.states[a].confirmed_position_shares > 0:
            await coordinator.apply(ReconciliationObserved(
                **new_envelope(session_id=a, dedupe_key=f"{CORRECTION}:reconcile:{a}"),
                source="ownership_correction", complete=True,
                confirmed_position_shares=Decimal(0), sellable_shares=Decimal(0),
                open_order_ids=(),
                notes=(
                    "never_sent_attempt:no_post_attempt_no_venue_order_no_trade",
                    f"shares_owned_by:{b}",
                    *(f"supersedes_event:{eid}" for eid in plan.false_observation_ids),
                ),
            ))
            report["actions"].append(f"journal {a}: ReconciliationObserved 0 (correction)")
        # 2. Commit the never-sold position to resolution in the ledger.
        ep = ledger.episodes[ep_id]
        if ep.state is S.FILLED:
            ledger.record("HOLD_REQUESTED", ep_id, key=f"HOLD_REQUESTED:{ep_id}:{CORRECTION}",
                          quantity=str(ep.net_shares), reason=REASON)
            report["actions"].append(f"ledger {ep_id}: HOLD_REQUESTED (correction)")
        if ledger.episodes[ep_id].state is S.HOLD_REQUESTED:
            ledger.record("HOLD_COMMITTED", ep_id, key=f"HOLD_COMMITTED:{ep_id}:{CORRECTION}",
                          quantity=str(ep.net_shares), reason=REASON)
            report["actions"].append(f"ledger {ep_id}: HOLD_COMMITTED (correction)")
        # 3. Finalized payout through the established resolution workflow.
        if connect_chain:
            from tyrex_pm.application.settlement_cli import _chain_manager

            manager = _chain_manager(ledger, config, dotenv)
            outcome = await manager.reconcile()
            report["actions"].append(f"settlement reconcile: {outcome.__dict__}")
        ep = ledger.episodes[ep_id]
        # 4. Settle the filled attempt's journal holding with the proven payout.
        settled = coordinator.states[b].settlement_holding_shares >= plan.shares
        if ep.payout_per_share is not None and settled:
            report["actions"].append(f"journal {b}: settlement already recorded")
        elif ep.payout_per_share is not None:
            proof = f"ledger:{ep_id}:PAYOUT_PROVEN:payout={ep.payout_per_share}"
            await coordinator.apply(MarketSettlementObserved(
                **new_envelope(session_id=b, dedupe_key=(
                    f"settlement:{plan.condition_id}:{plan.token_id}:{proof}")),
                condition_id=plan.condition_id, token_id=plan.token_id,
                holding_shares=plan.shares, payout_per_share=ep.payout_per_share,
                proof_source=proof, supported=True,
            ))
            report["actions"].append(f"journal {b}: MarketSettlementObserved ({proof})")
        else:
            report["actions"].append(
                "payout not proven yet: rerun with --connect-chain once finalized")
        report["after"] = {"never_sent": _session_summary(coordinator.states[a]),
                           "filled": _session_summary(coordinator.states[b]),
                           "episode": _episode_summary(ledger.episodes[ep_id])}
        await coordinator.close()
        return report
    finally:
        writer.release()
        lock.release()


def run(args: Any) -> int:
    from tyrex_pm.runtime.run_config import load_trading_run_config

    config = load_trading_run_config(args.config)
    try:
        report = asyncio.run(run_repair(
            config, args.window, apply=args.apply, connect_chain=args.connect_chain,
            dotenv=getattr(args, "dotenv", None)))
    except RepairRefused as exc:
        print(json.dumps({"refused": str(exc)}, indent=1))
        return 2
    print(json.dumps(report, indent=1, default=str))
    return 0
