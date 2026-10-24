"""Constants and helpers shared by the trading-runtime host components."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.execution.session_state import (
    ExecutionPhase,
    ExecutionSessionState,
)
from tyrex_pm.runtime.market_runtime import ZGapMarketRuntime
from tyrex_pm.strategies.context import (
    StrategyLifecycleSnapshot,
    StrategyPositionPhase,
)

# Late-evidence watch of released entry attempts in the current window [s].
LATE_EVIDENCE_WATCH_S = 60.0


LATE_EVIDENCE_POLL_S = 5.0


# Soft rewarm before official SDK AsyncOrderMetadataCache TTL (10 minutes).
_ORDER_METADATA_WARM_TTL_S = 8 * 60


# Refresh order metadata this long before it expires, also while its market is active.
_ORDER_METADATA_REFRESH_LEAD_S = 90.0


_ORDER_METADATA_REFRESH_MAX_ATTEMPTS = 5


_ORDER_METADATA_RETRY_BASE_S = 2.0


_ORDER_METADATA_RETRY_MAX_S = 30.0


_ORDER_METADATA_WARM_MAX_PRICE = Decimal("0.99")


_ORDER_METADATA_WARM_AMOUNT = Decimal("1")


_PREPARE_TIMING_FIELDS = (
    "prepare_queue_wait_ms",
    "tick_resolve_ms",
    "create_market_order_ms",
    "refresh_wait_ms",
    "refresh_elapsed_ms",
)


def _try_execution_epoch(coordinator: object):
    from tyrex_pm.execution.inspection_sync import capture_execution_epoch

    try:
        return capture_execution_epoch(coordinator)
    except Exception:
        return None


def _merge_prepare_timings(row: dict[str, Any], event: Any) -> None:
    """Copy a measured field. ``None`` leaves the earlier measurement in place."""
    for name in _PREPARE_TIMING_FIELDS:
        incoming = getattr(event, name, None)
        if incoming is not None:
            row[name] = incoming
    miss = getattr(event, "order_metadata_cache_miss", None)
    if miss is not None:
        row["order_metadata_cache_miss"] = bool(miss)


def _lifecycle_view(
    state: ExecutionSessionState | None, *, instrument_id=None
) -> StrategyLifecycleSnapshot:
    phase = ExecutionPhase.FLAT if state is None else state.phase
    mapping = {
        ExecutionPhase.NEW: StrategyPositionPhase.FLAT,
        ExecutionPhase.FLAT: StrategyPositionPhase.FLAT,
        ExecutionPhase.ENTRY_PREPARING: StrategyPositionPhase.ENTRY_PENDING,
        ExecutionPhase.ENTRY_DISPATCHING: StrategyPositionPhase.ENTRY_PENDING,
        ExecutionPhase.POSITION_PENDING_CONFIRMATION: StrategyPositionPhase.ENTRY_PENDING,
        ExecutionPhase.POSITION_OPEN: StrategyPositionPhase.ACTIVE,
        ExecutionPhase.EXIT_PREPARING: StrategyPositionPhase.EXIT_REQUESTED,
        ExecutionPhase.EXIT_DISPATCHING: StrategyPositionPhase.EXIT_PENDING,
        ExecutionPhase.RECONCILING: (
            StrategyPositionPhase.ACTIVE
            if state is not None and state.has_exposure
            else StrategyPositionPhase.FLAT
        ),
        ExecutionPhase.COMPLETED_NO_DISPATCH: StrategyPositionPhase.FLAT,
        ExecutionPhase.COMPLETED_NO_FILL: StrategyPositionPhase.FLAT,
        ExecutionPhase.COMPLETED_FLAT: StrategyPositionPhase.FLAT,
        ExecutionPhase.COMPLETED_FLAT_WITH_DUST: StrategyPositionPhase.FLAT,
        ExecutionPhase.MANUAL_INTERVENTION: StrategyPositionPhase.MANUAL_INTERVENTION,
    }
    return StrategyLifecycleSnapshot(
        phase=mapping[phase],
        instrument_id=instrument_id,
    )


def _hold_committed(runtime: Any) -> bool:
    """True only for a hold-profile run whose active window's holding is committed."""
    hooks = getattr(runtime, "settlement_hooks", None)
    session = getattr(runtime, "active_market", None)
    if hooks is None or session is None:
        return False
    return hooks.is_committed(session.window_id)


RECORDING_STATUS_INTERVAL_S = 60.0


# Decision-row fields kept in the STRATEGY_DECISION journal event when the full row is
# in the decision recording (watch-run and the run report read these).
_JOURNAL_DECISION_FIELDS = (
    "tau_s", "p_up_point", "up_ask", "up_bid", "down_ask", "down_bid", "chosen_side",
    "episode_seq", "attempt_seq", "episode_phase", "disagreement_gap_pp",
)


def _compact_decision_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    row = evidence.get("decision_row")
    if not isinstance(row, dict):
        return evidence
    return {**evidence, "decision_row": {k: row[k] for k in _JOURNAL_DECISION_FIELDS
                                         if k in row}, "decision_row_compact": True}


def _recording_status_event(status: dict[str, Any]) -> dict[str, Any]:
    """Compact status for the run journal (counters, no gap lists)."""
    streams = {}
    for name, snap in (status.get("streams") or {}).items():
        streams[name] = {key: value for key, value in snap.items() if key != "gaps"}
        streams[name]["gap_count"] = len(snap.get("gaps") or ())
    return {
        "failed": status.get("failed"), "dropped": status.get("dropped"),
        "writer_alive": status.get("writer_alive"),
        "writer_cpu_s": status.get("writer_cpu_s"),
        "capture_cost_us": status.get("capture_cost_us"),
        "capture_errors": status.get("capture_errors"),
        "undeclared_fields": status.get("undeclared_fields"),
        "streams": streams, "run_evidence": status.get("run_evidence"),
        "error": status.get("error"),
    }


# Hold refusals that clear by themselves (trades confirming, a cancel being proven).
_TRANSIENT_HOLD_REFUSALS = frozenset(
    {"NO_CONFIRMED_INVENTORY", "INVENTORY_DIFFERS_FROM_LEDGER",
     "CONFLICTING_ORDER_NOT_PROVEN_CANCELLED", "UNRESOLVED_MUTATION"}
)


def _chain_reader(url: str | None = None):
    """Read-only Polygon settlement reader for ``url`` (TYREX_POLYGON_RPC_URL), or None."""
    try:
        from tyrex_pm.adapters.polymarket.polygon_rpc import POLYGON_RPC_ENV
        from tyrex_pm.execution.settlement.chain import SettlementChainReader
    except Exception:  # noqa: BLE001 - optional live dependency
        return None
    url = url or os.environ.get(POLYGON_RPC_ENV, "")
    if not url:
        return None
    return SettlementChainReader(url)


def _chain_payout_reader(url: str | None = None, reader: Any = None):
    """Read-only finalized payout reader for ``url`` (TYREX_POLYGON_RPC_URL), or None."""
    reader = reader or _chain_reader(url)
    if reader is None:
        return None
    from tyrex_pm.execution.settlement.chain import finalized_payout_reader

    return finalized_payout_reader(reader)


def ready_binding_strategy_id(runtime: ZGapMarketRuntime):
    return runtime.zgap.strategy_id


def ready_binding_strategy_id_from_session(state: ExecutionSessionState):
    from tyrex_pm.core.ids import StrategyId

    if state.identity is None:
        raise RuntimeError("restored session has no identity")
    return StrategyId(state.identity.strategy_id)


def ready_fallback_correlation():
    from tyrex_pm.core.ids import new_correlation_id

    return new_correlation_id()


@dataclass(frozen=True)
class TradingRunResult:
    outcome: str
    ok: bool
    report_path: Path
    mutation_attempts: int | None
    state: ExecutionSessionState | None


def _recovery_observation(facts: Any, proposal: Any):
    """Shares and lifecycle from a fresh read. None means the recovery must stop."""
    from tyrex_pm.execution.startup_reconciliation import _holdings_changed, _orders_changed

    if facts is None or facts.identity is None or not facts.coverage.complete:
        return None
    if _holdings_changed(facts.holdings, facts.closing_holdings):
        return None
    if _orders_changed(facts.open_orders, facts.closing_open_orders):
        return None
    shares = Decimal("0")
    for holding in facts.holdings:
        if holding.shares <= 0 or holding.token_id != proposal.token_id:
            continue
        if holding.condition_id != proposal.condition_id:
            return None
        shares += holding.shares
    allowed_orders = set(proposal.venue_order_ids)
    for order in facts.open_orders:
        if order.token_id != proposal.token_id or str(order.side).upper() != "BUY":
            continue
        if order.venue_order_id not in allowed_orders:
            return None
    lifecycle = facts.lifecycles.get(proposal.condition_id)
    status = "" if lifecycle is None else lifecycle.status
    return shares, status


async def _tty_decision_reader(prompt: str) -> str | None:
    """Read one operator line. A non-TTY or EOF declines; it never approves."""
    import sys

    if sys.stdin is None or not sys.stdin.isatty():
        return None
    try:
        return await asyncio.to_thread(input, prompt)
    except EOFError:
        return None
