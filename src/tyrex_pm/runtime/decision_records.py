"""One decision-context builder and one decision-record schema for replay and observation.

Offline replay (``research.strategy_replay``) and signal-only live observation
(``runtime.observation``) both build the strategy's ``DecisionContext`` and the
recorded row through these functions, so their outputs can be compared directly.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tyrex_pm.core.ids import InstrumentId, RunId
from tyrex_pm.strategies.context import (
    DecisionContext,
    ReferenceFacts,
    StrategyLifecycleSnapshot,
    StrategyPositionPhase,
)

RECORD_SCHEMA = "settlement_decision_record_v1"


def reference_facts(ready: Any) -> ReferenceFacts:
    dyn = ready.dyn
    return ReferenceFacts(
        chainlink_raw=None if dyn is None else dyn.chainlink_raw,
        chainlink_fresh=dyn is not None and dyn.chainlink_raw is not None,
        sealed_k=None if ready.sealed is None else ready.sealed.ptb_k,
        trading_reference_raw=ready.binance_raw,
        chainlink_source_ts=None if dyn is None else dyn.chainlink_source_ts,
    )


def decision_context(
    ready: Any,
    *,
    run_id: str,
    target_notional: Decimal,
    position_quantity: Decimal = Decimal("0"),
    position_cost_total: Decimal = Decimal("0"),
    position_instrument: InstrumentId | None = None,
    pending: bool = False,
    exit_pending: bool = False,
    last_exit_reason: str | None = None,
) -> DecisionContext:
    """Context from the host's aligned evaluation and an execution-derived position view."""
    held = position_quantity > 0
    if exit_pending:
        phase = StrategyPositionPhase.EXIT_PENDING
    elif pending:
        phase = StrategyPositionPhase.ENTRY_PENDING
    elif held:
        phase = StrategyPositionPhase.ACTIVE
    else:
        phase = StrategyPositionPhase.FLAT
    busy = pending or exit_pending
    return DecisionContext(
        run_id=RunId(run_id),
        snapshot=ready.snapshot,
        target_notional=target_notional,
        lifecycle=StrategyLifecycleSnapshot(phase=phase, instrument_id=position_instrument),
        position_quantity=position_quantity,
        position_cost_total=position_cost_total,
        now=ready.snapshot.observed_at,
        entry_allowed=not busy and not held,
        entry_block_reason="pending_order" if busy else None,
        references=reference_facts(ready),
        last_exit_reason=last_exit_reason,
    )


def base_record(ready: Any, *, at_ns: int, window_end_ns: int | None) -> dict[str, Any]:
    """Host-side fields recorded for every evaluation, including host skips."""
    return {
        "schema": RECORD_SCHEMA,
        "at_ns": at_ns,
        "tau_s": None if window_end_ns is None else (window_end_ns - at_ns) / 1e9,
        "binance_raw": None if ready.binance_raw is None else str(ready.binance_raw),
        "model_spot": None if ready.model_spot is None else str(ready.model_spot),
        "chainlink_raw": (
            None
            if ready.dyn is None or ready.dyn.chainlink_raw is None
            else str(ready.dyn.chainlink_raw)
        ),
        "sealed_k": None if ready.sealed is None else str(ready.sealed.ptb_k),
    }


def skipped_record(ready: Any, *, at_ns: int, window_end_ns: int | None) -> dict[str, Any]:
    return {
        **base_record(ready, at_ns=at_ns, window_end_ns=window_end_ns),
        "action": "HOST_SKIPPED",
        "reason": ",".join(ready.skip_reasons) or "not_ready",
    }


def evaluated_record(
    ready: Any, result: Any, *, at_ns: int, window_end_ns: int | None
) -> dict[str, Any]:
    row = dict(result.reporting_context.get("decision_row") or {})
    row.update(base_record(ready, at_ns=at_ns, window_end_ns=window_end_ns))
    row.setdefault("action", result.decision.action.value)
    row.setdefault("reason", result.decision.reason_code)
    return row
