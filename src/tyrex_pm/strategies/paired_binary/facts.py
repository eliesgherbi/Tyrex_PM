"""Paired binary fact emission with dedup (Phase 4.6)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tyrex_pm.core.ids import RunId
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_PAIRED_BINARY_DONE,
    FACT_TYPE_PAIRED_BINARY_ENTRY_EVAL,
    FACT_TYPE_PAIRED_BINARY_ENTRY_QTY_RECONCILED,
    FACT_TYPE_PAIRED_BINARY_ENTRY_SKIP,
    FACT_TYPE_PAIRED_BINARY_ENTRY_SUBMITTED,
    FACT_TYPE_PAIRED_BINARY_ENTRY_TIMEOUT_UNWIND,
    FACT_TYPE_PAIRED_BINARY_ACTIVATION_REFERENCE,
    FACT_TYPE_PAIRED_BINARY_ACTIVATION_REJECTED_LOSS_BUDGET,
    FACT_TYPE_PAIRED_BINARY_ACTIVATION_GAP_RECHECK,
    FACT_TYPE_PAIRED_BINARY_ACTIVATION_RECOVERED,
    FACT_TYPE_PAIRED_BINARY_ENTRY_PRICE_UNKNOWN,
    FACT_TYPE_PAIRED_BINARY_ENTRY_PRICE_MISMATCH,
    FACT_TYPE_PAIRED_BINARY_EMERGENCY_UNWIND_STARTED,
    FACT_TYPE_PAIRED_BINARY_EMERGENCY_UNWIND_ATTEMPT,
    FACT_TYPE_PAIRED_BINARY_EMERGENCY_UNWIND_BLOCKED,
    FACT_TYPE_PAIRED_BINARY_EMERGENCY_UNWIND_RETRY,
    FACT_TYPE_PAIRED_BINARY_EMERGENCY_UNWIND_DONE,
    FACT_TYPE_PAIRED_BINARY_MANUAL_INTERVENTION_REQUIRED,
    FACT_TYPE_PAIRED_BINARY_LATENCY_SAMPLE,
    FACT_TYPE_PAIRED_BINARY_BOOK_CAPTURE_QUALITY,
    FACT_TYPE_PAIRED_BINARY_PAIR_PREFLIGHT,
    FACT_TYPE_PAIRED_BINARY_PAIR_PREFLIGHT_REJECTED,
    FACT_TYPE_PAIRED_BINARY_ENTRY_ORDER_STYLE_APPLIED,
    FACT_TYPE_PAIRED_BINARY_ENTRY_LEG_BLOCKED,
    FACT_TYPE_PAIRED_BINARY_ENTRY_ASYMMETRY_DETECTED,
    FACT_TYPE_PAIRED_BINARY_ENTRY_CANCEL_ATTEMPT,
    FACT_TYPE_PAIRED_BINARY_ENTRY_CANCEL_ACK,
    FACT_TYPE_PAIRED_BINARY_ENTRY_CANCEL_FAILED,
    FACT_TYPE_PAIRED_BINARY_PAIR_ENTRY_COMMITTED,
    FACT_TYPE_PAIRED_BINARY_PAIR_ENTRY_ABORTED_FLAT,
    FACT_TYPE_PAIRED_BINARY_PAIR_ENTRY_MANUAL_INTERVENTION,
    FACT_TYPE_PAIRED_BINARY_ENTRY_TIMEOUT_UNWIND_RETRY,
    FACT_TYPE_PAIRED_BINARY_EXIT_RETRY,
    FACT_TYPE_PAIRED_BINARY_EXIT_STATE_RECOVERED,
    FACT_TYPE_PAIRED_BINARY_EXIT_SUBMIT_ATTEMPT,
    FACT_TYPE_PAIRED_BINARY_EXIT_SUBMIT_BLOCKED,
    FACT_TYPE_PAIRED_BINARY_EXIT_TRIGGER_PENDING,
    FACT_TYPE_PAIRED_BINARY_LEG_STOP,
    FACT_TYPE_PAIRED_BINARY_MONITOR_STARTED,
    FACT_TYPE_PAIRED_BINARY_PNL_PLAN,
    FACT_TYPE_PAIRED_BINARY_REALIZED_PNL,
    FACT_TYPE_PAIRED_BINARY_REALIZED_PNL_UNAVAILABLE,
    FACT_TYPE_PAIRED_BINARY_PRICE_BASED_PNL_ESTIMATE,
    FACT_TYPE_PAIRED_BINARY_RECOVERED,
    FACT_TYPE_PAIRED_BINARY_STATE_CHANGE,
    FACT_TYPE_PAIRED_BINARY_STOP_PLAN,
    FACT_TYPE_PAIRED_BINARY_TIMEOUT_EXIT,
    FACT_TYPE_PAIRED_BINARY_UNWIND,
    FACT_TYPE_PAIRED_BINARY_WAITING_SELLABLE,
    FACT_TYPE_PAIRED_BINARY_WINNER_TARGET,
    FACT_TYPE_PAIRED_BINARY_WINNER_TARGET_PLAN,
    FACT_TYPE_PAIRED_BINARY_WINNER_TARGET_REPRICED,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import PairedBinaryStrategyConfig
from tyrex_pm.strategies.paired_binary.entry_eval import EntryEvalInput, LegBook
from tyrex_pm.strategies.paired_binary.exit_engine import ExitTriggerContext, LegSellability
from tyrex_pm.strategies.paired_binary.pnl import (
    price_based_pnl_estimate_from_stored_prices,
    realized_pnl_from_cashflows,
)
from tyrex_pm.strategies.paired_binary.state import (
    PairedBinaryRuntimeState,
    leg_entry_avg_price,
    leg_exit_avg_price,
)
from tyrex_pm.runtime.entry_qty_reconcile import LegEntryQtyReconcile, PairEntryQtyReconcile
from tyrex_pm.strategies.paired_binary.entry_price import EntryPriceMismatch
from tyrex_pm.strategies.paired_binary.emergency_unwind import UnwindLegResult


def _price_bucket(v: Decimal | None, places: int = 4) -> str:
    if v is None:
        return "na"
    q = Decimal("1").scaleb(-places)
    return str(v.quantize(q))


def _entry_economics_payload(
    *,
    pair_cost: Decimal | None,
    yes_spread: Decimal | None,
    no_spread: Decimal | None,
    estimated_loss_budget: Decimal | None = None,
    slippage_buffer: Decimal | None = None,
) -> dict[str, Any]:
    return {
        "pair_cost": str(pair_cost) if pair_cost is not None else None,
        "yes_spread": str(yes_spread) if yes_spread is not None else None,
        "no_spread": str(no_spread) if no_spread is not None else None,
        "estimated_pair_cost": str(pair_cost) if pair_cost is not None else None,
        "estimated_loss_budget": str(estimated_loss_budget) if estimated_loss_budget is not None else None,
        "yes_entry_spread": str(yes_spread) if yes_spread is not None else None,
        "no_entry_spread": str(no_spread) if no_spread is not None else None,
        "slippage_buffer": str(slippage_buffer) if slippage_buffer is not None else None,
    }


def _book_payload(yes: LegBook, no: LegBook) -> dict[str, str]:
    return {
        "yes_bid": str(yes.bid) if yes.bid is not None else None,
        "yes_ask": str(yes.ask) if yes.ask is not None else None,
        "no_bid": str(no.bid) if no.bid is not None else None,
        "no_ask": str(no.ask) if no.ask is not None else None,
    }


def base_payload(state: PairedBinaryRuntimeState, yes: LegBook, no: LegBook) -> dict[str, Any]:
    out: dict[str, Any] = {
        "market_id": state.market_id,
        "yes_token_id": state.yes_token_id,
        "no_token_id": state.no_token_id,
        "owner_id": state.owner_id,
        "state": state.phase.value,
        "pair_correlation_id": state.pair_correlation_id,
        "yes_entry": str(state.yes_entry) if state.yes_entry is not None else None,
        "no_entry": str(state.no_entry) if state.no_entry is not None else None,
        "yes_entry_price_source": state.yes_entry_price_source,
        "no_entry_price_source": state.no_entry_price_source,
        "entry_price_source": state.entry_price_source,
        "pair_cost": str(state.pair_cost) if state.pair_cost is not None else None,
        "loss_budget": str(state.loss_budget) if state.loss_budget is not None else None,
        "profit_budget": str(state.profit_budget) if state.profit_budget is not None else None,
        "desired_net_profit_per_pair": (
            str(state.desired_net_profit_per_pair)
            if state.desired_net_profit_per_pair is not None
            else None
        ),
    }
    out.update(_book_payload(yes, no))
    return out


def should_emit(state: PairedBinaryRuntimeState, dedup_key: str) -> bool:
    if dedup_key in state.dedup_keys:
        return False
    state.dedup_keys.add(dedup_key)
    return True


def _write_fact(sink: JsonlSink | None, obj: dict) -> None:
    if sink is not None:
        sink.write(obj)


def emit_entry_eval(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    pair_cost: Decimal,
    yes_spread: Decimal,
    no_spread: Decimal,
    estimated_loss_budget: Decimal | None = None,
    slippage_buffer: Decimal | None = None,
) -> None:
    dedup = f"entry_eval:{_price_bucket(pair_cost)}"
    if not should_emit(state, dedup):
        return
    payload = base_payload(state, yes, no)
    payload.update(
        _entry_economics_payload(
            pair_cost=pair_cost,
            yes_spread=yes_spread,
            no_spread=no_spread,
            estimated_loss_budget=estimated_loss_budget,
            slippage_buffer=slippage_buffer,
        )
    )
    _write_fact(sink, make_fact(FACT_TYPE_PAIRED_BINARY_ENTRY_EVAL, str(run_id), payload))


def emit_entry_skip(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    reason: str,
    pair_cost: Decimal | None,
    yes_spread: Decimal | None,
    no_spread: Decimal | None,
    estimated_loss_budget: Decimal | None = None,
    slippage_buffer: Decimal | None = None,
) -> None:
    dedup = f"entry_skip:{reason}:{_price_bucket(pair_cost)}"
    if not should_emit(state, dedup):
        return
    payload = base_payload(state, yes, no)
    payload.update({"reason": reason})
    payload.update(
        _entry_economics_payload(
            pair_cost=pair_cost,
            yes_spread=yes_spread,
            no_spread=no_spread,
            estimated_loss_budget=estimated_loss_budget,
            slippage_buffer=slippage_buffer,
        )
    )
    _write_fact(sink, make_fact(FACT_TYPE_PAIRED_BINARY_ENTRY_SKIP, str(run_id), payload))


def emit_entry_submitted(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    pair_cost: Decimal,
    yes_spread: Decimal,
    no_spread: Decimal,
) -> None:
    dedup = "entry_submitted"
    if not should_emit(state, dedup):
        return
    payload = base_payload(state, yes, no)
    payload.update(
        {
            "pair_cost": str(pair_cost),
            "yes_spread": str(yes_spread),
            "no_spread": str(no_spread),
            "yes_leg_correlation_id": state.yes.leg_correlation_id,
            "no_leg_correlation_id": state.no.leg_correlation_id,
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_ENTRY_SUBMITTED,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        )
    )


def emit_state_change(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    from_phase: str,
    to_phase: str,
    reason: str | None = None,
) -> None:
    dedup = f"state:{from_phase}:{to_phase}:{reason or ''}"
    if not should_emit(state, dedup):
        return
    payload = base_payload(state, yes, no)
    payload.update({"from": from_phase, "to": to_phase, "reason": reason})
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_STATE_CHANGE,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        )
    )


def emit_leg_stop(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    leg: str,
    target_price: Decimal | None,
) -> None:
    dedup = f"leg_stop:{leg}:{_price_bucket(target_price)}"
    if not should_emit(state, dedup):
        return
    payload = base_payload(state, yes, no)
    payload.update({"leg": leg, "active_leg": leg, "target_price": str(target_price) if target_price else None})
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_LEG_STOP,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        )
    )


def emit_winner_target(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    leg: str,
    target_price: Decimal,
) -> None:
    dedup = f"winner:{leg}:{_price_bucket(target_price)}"
    if not should_emit(state, dedup):
        return
    payload = base_payload(state, yes, no)
    payload.update({"leg": leg, "active_leg": leg, "target_price": str(target_price)})
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_WINNER_TARGET,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        )
    )


def emit_timeout_exit(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
) -> None:
    if not should_emit(state, "timeout_exit"):
        return
    payload = base_payload(state, yes, no)
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_TIMEOUT_EXIT,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        )
    )


def emit_unwind(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    leg: str,
    reason: str,
    qty: Decimal,
) -> None:
    dedup = f"unwind:{leg}:{reason}:{qty}"
    if not should_emit(state, dedup):
        return
    payload = base_payload(state, yes, no)
    payload.update({"leg": leg, "reason": reason, "qty": str(qty)})
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_UNWIND,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        )
    )


def emit_done(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
) -> None:
    if not should_emit(state, "done"):
        return
    payload = base_payload(state, yes, no)
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_DONE,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        )
    )


def emit_recovered(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    recovery_source: str,
    recovery_action: str | None = None,
) -> None:
    payload = base_payload(state, yes, no)
    payload.update({"recovery_source": recovery_source, "entry_price_source": state.entry_price_source})
    if recovery_action is not None:
        payload["recovery_action"] = recovery_action
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_RECOVERED,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        )
    )


def _leg_qty_payload(leg: LegEntryQtyReconcile) -> dict[str, Any]:
    return {
        "leg": leg.leg,
        "token_id": str(leg.token_id),
        "confirmed_qty": str(leg.confirmed_qty),
        "ledger_qty_before": str(leg.ledger_qty),
        "ledger_qty_after": str(leg.effective_qty if leg.repaired else leg.ledger_qty),
        "venue_qty": str(leg.venue_qty),
        "effective_qty": str(leg.effective_qty),
        "source": leg.source,
        "repaired": leg.repaired,
        "leg_correlation_id": leg.leg_correlation_id,
    }


def emit_entry_qty_reconciled(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    pair: PairEntryQtyReconcile,
    reason: str,
) -> None:
    dedup = f"entry_qty_reconciled:{reason}"
    if not should_emit(state, dedup):
        return
    payload = base_payload(state, yes, no)
    payload.update(
        {
            "reason": reason,
            "effective_pair_qty": str(pair.effective_pair_qty),
            "yes": _leg_qty_payload(pair.yes),
            "no": _leg_qty_payload(pair.no),
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_ENTRY_QTY_RECONCILED,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_entry_timeout_unwind(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    leg: str,
    qty: Decimal,
    reason: str,
) -> None:
    dedup = f"entry_timeout_unwind:{leg}:{qty}"
    if not should_emit(state, dedup):
        return
    payload = base_payload(state, yes, no)
    payload.update({"leg": leg, "qty": str(qty), "reason": reason})
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_ENTRY_TIMEOUT_UNWIND,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_entry_timeout_unwind_retry(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    reason: str,
    blocked_leg: str,
) -> None:
    payload = base_payload(state, yes, no)
    payload.update({"reason": reason, "blocked_leg": blocked_leg})
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_ENTRY_TIMEOUT_UNWIND_RETRY,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )

def _activation_gap_payload(
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    cfg: PairedBinaryStrategyConfig,
    *,
    attempt_count: int | None = None,
    elapsed_s: float | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    yes_gap = (
        (state.yes_entry - yes.bid)
        if state.yes_entry is not None and yes.bid is not None
        else None
    )
    no_gap = (
        (state.no_entry - no.bid) if state.no_entry is not None and no.bid is not None else None
    )
    allowed_gap = (
        (state.loss_budget + cfg.slippage_buffer)
        if state.loss_budget is not None
        else None
    )
    payload = base_payload(state, yes, no)
    ages = [b for b in (yes.book_age_ms, no.book_age_ms) if b is not None]
    payload.update(
        {
            "yes_bid": str(yes.bid) if yes.bid is not None else None,
            "no_bid": str(no.bid) if no.bid is not None else None,
            "yes_gap": str(yes_gap) if yes_gap is not None else None,
            "no_gap": str(no_gap) if no_gap is not None else None,
            "loss_budget": str(state.loss_budget) if state.loss_budget is not None else None,
            "slippage_buffer": str(cfg.slippage_buffer),
            "allowed_gap": str(allowed_gap) if allowed_gap is not None else None,
            "attempt_count": attempt_count,
            "elapsed_s": elapsed_s,
            "reason": reason,
            "activation_book_age_ms": max(ages) if ages else None,
        }
    )
    return payload


def emit_activation_gap_recheck(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    cfg: PairedBinaryStrategyConfig,
    *,
    attempt_count: int,
    elapsed_s: float,
    reason: str,
) -> None:
    payload = _activation_gap_payload(
        state, yes, no, cfg, attempt_count=attempt_count, elapsed_s=elapsed_s, reason=reason
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_ACTIVATION_GAP_RECHECK,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_activation_recovered(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    cfg: PairedBinaryStrategyConfig,
    *,
    attempt_count: int,
    elapsed_s: float,
) -> None:
    if not should_emit(state, "activation_recovered"):
        return
    payload = _activation_gap_payload(
        state,
        yes,
        no,
        cfg,
        attempt_count=attempt_count,
        elapsed_s=elapsed_s,
        reason="activation_recovered",
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_ACTIVATION_RECOVERED,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_entry_price_unknown(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    leg: str,
) -> None:
    payload = base_payload(state, yes, no)
    payload.update({"leg": leg, "reason": "entry_price_unknown"})
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_ENTRY_PRICE_UNKNOWN,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_entry_price_mismatch(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    mismatch: EntryPriceMismatch,
) -> None:
    payload = base_payload(state, yes, no)
    payload.update(
        {
            "leg": mismatch.leg,
            "oms_price": str(mismatch.oms_price) if mismatch.oms_price is not None else None,
            "ws_price": str(mismatch.ws_price) if mismatch.ws_price is not None else None,
            "reconcile_price": (
                str(mismatch.reconcile_price) if mismatch.reconcile_price is not None else None
            ),
            "chosen_price": str(mismatch.chosen_price),
            "chosen_source": mismatch.chosen_source,
            "delta": str(mismatch.delta),
            "tolerance": str(mismatch.tolerance),
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_ENTRY_PRICE_MISMATCH,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_emergency_unwind_started(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    reason: str,
) -> None:
    if not should_emit(state, f"emergency_unwind_started:{reason}"):
        return
    payload = base_payload(state, yes, no)
    payload.update({"reason": reason})
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_EMERGENCY_UNWIND_STARTED,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_emergency_unwind_attempt(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    result: UnwindLegResult,
    attempt_count: int,
    reason: str,
) -> None:
    payload = base_payload(state, yes, no)
    payload.update(
        {
            "leg": result.leg,
            "reason": reason,
            "attempt_count": attempt_count,
            "allocation_qty": str(result.allocation_qty),
            "venue_available_qty": str(result.venue_available_qty),
            "book_bid": str(result.book_bid) if result.book_bid is not None else None,
            "book_age_ms": result.book_age_ms,
            "risk_reason": result.risk_reason,
            "submitted": result.submitted,
            "blocked": result.blocked,
            "final_size": str(result.final_size),
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_EMERGENCY_UNWIND_ATTEMPT,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_emergency_unwind_blocked(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    result: UnwindLegResult,
    attempt_count: int,
    reason: str,
) -> None:
    payload = base_payload(state, yes, no)
    payload.update(
        {
            "leg": result.leg,
            "reason": result.risk_reason or reason,
            "attempt_count": attempt_count,
            "book_age_ms": result.book_age_ms,
            "risk_reason": result.risk_reason,
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_EMERGENCY_UNWIND_BLOCKED,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_emergency_unwind_retry(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    attempt_count: int,
    reason: str,
) -> None:
    payload = base_payload(state, yes, no)
    payload.update({"attempt_count": attempt_count, "reason": reason})
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_EMERGENCY_UNWIND_RETRY,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_emergency_unwind_done(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    attempt_count: int,
    reason: str,
) -> None:
    if not should_emit(state, "emergency_unwind_done"):
        return
    payload = base_payload(state, yes, no)
    payload.update({"attempt_count": attempt_count, "reason": reason})
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_EMERGENCY_UNWIND_DONE,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_manual_intervention_required(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    attempt_count: int,
    reason: str,
) -> None:
    if not should_emit(state, "manual_intervention_required"):
        return
    payload = base_payload(state, yes, no)
    payload.update({"attempt_count": attempt_count, "reason": reason})
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_MANUAL_INTERVENTION_REQUIRED,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_latency_sample(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    *,
    payload: dict[str, Any],
) -> None:
    payload = {**payload, "pair_correlation_id": state.pair_correlation_id}
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_LATENCY_SAMPLE,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_book_capture_quality(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    event: str,
) -> None:
    payload = base_payload(state, yes, no)
    payload.update(
        {
            "event": event,
            "yes_book_age_ms": yes.book_age_ms,
            "no_book_age_ms": no.book_age_ms,
            "yes_book_update_ts": yes.book_update_ts,
            "no_book_update_ts": no.book_update_ts,
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_BOOK_CAPTURE_QUALITY,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )



def emit_monitor_started(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
) -> None:
    if not should_emit(state, "monitor_started"):
        return
    payload = base_payload(state, yes, no)
    payload.update({"effective_qty": str(state.effective_qty), "phase": state.phase.value})
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_MONITOR_STARTED,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def _exit_context_payload(
    state: PairedBinaryRuntimeState,
    *,
    leg: str,
    trigger_type: str,
    trigger_price: Decimal | None = None,
    reference_price: Decimal | None = None,
    target_price: Decimal | None = None,
    planned_qty: Decimal | None = None,
    allocation_qty: Decimal | None = None,
    venue_available_qty: Decimal | None = None,
    final_size: Decimal | None = None,
    reason: str | None = None,
    ctx: ExitTriggerContext | None = None,
) -> dict[str, Any]:
    payload = {
        "market_id": state.market_id,
        "leg": leg,
        "phase": state.phase.value,
        "trigger_type": trigger_type,
        "pair_correlation_id": state.pair_correlation_id,
    }
    if ctx is not None:
        payload.update(
            {
                "trigger_price": str(ctx.trigger_price),
                "reference_price": str(ctx.reference_price),
                "target_price": str(ctx.target_price) if ctx.target_price is not None else None,
                "planned_qty": str(ctx.planned_qty),
                "allocation_qty": str(ctx.allocation_qty),
                "venue_available_qty": str(ctx.venue_available_qty),
                "final_size": str(ctx.final_size),
                "reason": ctx.reason,
            }
        )
    else:
        if trigger_price is not None:
            payload["trigger_price"] = str(trigger_price)
        if reference_price is not None:
            payload["reference_price"] = str(reference_price)
        if target_price is not None:
            payload["target_price"] = str(target_price)
        if planned_qty is not None:
            payload["planned_qty"] = str(planned_qty)
        if allocation_qty is not None:
            payload["allocation_qty"] = str(allocation_qty)
        if venue_available_qty is not None:
            payload["venue_available_qty"] = str(venue_available_qty)
        if final_size is not None:
            payload["final_size"] = str(final_size)
        if reason is not None:
            payload["reason"] = reason
    return payload


def emit_waiting_for_sellable_inventory(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    yes_sell: LegSellability,
    no_sell: LegSellability,
) -> None:
    dedup = f"waiting_sellable:{yes_sell.sellable_qty}:{no_sell.sellable_qty}"
    if not should_emit(state, dedup):
        return
    payload = base_payload(state, yes, no)
    payload.update(
        {
            "phase": state.phase.value,
            "planned_qty": str(state.effective_qty),
            "allocation_qty": str(min(yes_sell.allocation_qty, no_sell.allocation_qty)),
            "venue_available_qty": str(min(yes_sell.venue_available_qty, no_sell.venue_available_qty)),
            "final_size": str(min(yes_sell.sellable_qty, no_sell.sellable_qty)),
            "reason": "NO_SELLABLE_INVENTORY",
            "yes_allocation_qty": str(yes_sell.allocation_qty),
            "no_allocation_qty": str(no_sell.allocation_qty),
            "yes_venue_available_qty": str(yes_sell.venue_available_qty),
            "no_venue_available_qty": str(no_sell.venue_available_qty),
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_WAITING_SELLABLE,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_activation_reference(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
) -> None:
    if not should_emit(state, "activation_reference"):
        return
    payload = base_payload(state, yes, no)
    payload.update(
        {
            "phase": state.phase.value,
            "reference_price": str(state.yes_activation_bid) if state.yes_activation_bid else None,
            "yes_activation_bid": str(state.yes_activation_bid) if state.yes_activation_bid else None,
            "no_activation_bid": str(state.no_activation_bid) if state.no_activation_bid else None,
            "activation_ts": state.activation_ts,
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_ACTIVATION_REFERENCE,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_exit_trigger_pending(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    leg: str,
    trigger_type: str,
    ctx: ExitTriggerContext,
) -> None:
    dedup = f"exit_pending:{leg}:{trigger_type}:{ctx.reason}"
    if not should_emit(state, dedup):
        return
    payload = base_payload(state, yes, no)
    payload.update(_exit_context_payload(state, leg=leg, trigger_type=trigger_type, ctx=ctx))
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_EXIT_TRIGGER_PENDING,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_exit_submit_attempt(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    leg: str,
    trigger_type: str,
    trigger_price: Decimal,
    reference_price: Decimal | None = None,
    target_price: Decimal | None = None,
    planned_qty: Decimal | None = None,
    allocation_qty: Decimal | None = None,
    venue_available_qty: Decimal | None = None,
    final_size: Decimal | None = None,
) -> None:
    payload = base_payload(state, yes, no)
    payload.update(
        _exit_context_payload(
            state,
            leg=leg,
            trigger_type=trigger_type,
            trigger_price=trigger_price,
            reference_price=reference_price,
            target_price=target_price,
            planned_qty=planned_qty,
            allocation_qty=allocation_qty,
            venue_available_qty=venue_available_qty,
            final_size=final_size,
        )
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_EXIT_SUBMIT_ATTEMPT,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_exit_submit_blocked(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    leg: str,
    trigger_type: str,
    ctx: ExitTriggerContext,
) -> None:
    payload = base_payload(state, yes, no)
    payload.update(_exit_context_payload(state, leg=leg, trigger_type=trigger_type, ctx=ctx))
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_EXIT_SUBMIT_BLOCKED,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_exit_retry(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    leg: str,
    trigger_type: str,
    reason: str,
) -> None:
    payload = base_payload(state, yes, no)
    payload.update(
        {
            "leg": leg,
            "phase": state.phase.value,
            "trigger_type": trigger_type,
            "reason": reason,
            "pair_correlation_id": state.pair_correlation_id,
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_EXIT_RETRY,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_exit_state_recovered(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    from_phase: str,
    to_phase: str,
    reason: str,
) -> None:
    payload = base_payload(state, yes, no)
    payload.update(
        {
            "from": from_phase,
            "to": to_phase,
            "reason": reason,
            "phase": state.phase.value,
            "pair_correlation_id": state.pair_correlation_id,
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_EXIT_STATE_RECOVERED,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_pnl_plan(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    cfg: PairedBinaryStrategyConfig,
    yes_spread: Decimal | None = None,
    no_spread: Decimal | None = None,
) -> None:
    if not should_emit(state, "pnl_plan"):
        return
    payload = base_payload(state, yes, no)
    yes_gap = (
        (state.yes_entry - yes.bid)
        if state.yes_entry is not None and yes.bid is not None
        else None
    )
    no_gap = (
        (state.no_entry - no.bid) if state.no_entry is not None and no.bid is not None else None
    )
    payload.update(
        {
            "pair_cost": str(state.pair_cost) if state.pair_cost is not None else None,
            "pair_stop_loss_pct": str(cfg.pair_stop_loss_pct),
            "pair_take_profit_pct": str(cfg.pair_take_profit_pct),
            "loss_budget": str(state.loss_budget) if state.loss_budget is not None else None,
            "profit_budget": str(state.profit_budget) if state.profit_budget is not None else None,
            "desired_net_profit_per_pair": (
                str(state.desired_net_profit_per_pair)
                if state.desired_net_profit_per_pair is not None
                else None
            ),
            "expected_pnl_total": (
                str(state.expected_pnl_total) if state.expected_pnl_total is not None else None
            ),
            "position_size": str(state.effective_qty),
            "slippage_buffer": str(cfg.slippage_buffer),
            "yes_entry_spread": str(yes_spread) if yes_spread is not None else None,
            "no_entry_spread": str(no_spread) if no_spread is not None else None,
            "yes_activation_bid": (
                str(state.yes_activation_bid) if state.yes_activation_bid is not None else None
            ),
            "no_activation_bid": (
                str(state.no_activation_bid) if state.no_activation_bid is not None else None
            ),
            "yes_immediate_exit_gap": str(yes_gap) if yes_gap is not None else None,
            "no_immediate_exit_gap": str(no_gap) if no_gap is not None else None,
            "yes_planned_stop": str(state.yes_planned_stop) if state.yes_planned_stop else None,
            "yes_trigger_stop": str(state.yes_trigger_stop) if state.yes_trigger_stop else None,
            "no_planned_stop": str(state.no_planned_stop) if state.no_planned_stop else None,
            "no_trigger_stop": str(state.no_trigger_stop) if state.no_trigger_stop else None,
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_PNL_PLAN,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_stop_plan(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    cfg: PairedBinaryStrategyConfig,
    leg: str,
) -> None:
    dedup = f"stop_plan:{leg}"
    if not should_emit(state, dedup):
        return
    loser_entry = state.yes_entry if leg == "yes" else state.no_entry
    loser_bid = yes.bid if leg == "yes" else no.bid
    planned = state.yes_planned_stop if leg == "yes" else state.no_planned_stop
    trigger = state.yes_trigger_stop if leg == "yes" else state.no_trigger_stop
    est_loss = (loser_entry - loser_bid) if loser_entry is not None and loser_bid is not None else None
    payload = base_payload(state, yes, no)
    payload.update(
        {
            "loser_leg": leg,
            "loser_entry": str(loser_entry) if loser_entry is not None else None,
            "loser_bid": str(loser_bid) if loser_bid is not None else None,
            "planned_stop_price": str(planned) if planned is not None else None,
            "trigger_stop_price": str(trigger) if trigger is not None else None,
            "loss_budget": str(state.loss_budget) if state.loss_budget is not None else None,
            "estimated_loss_if_filled_at_bid": str(est_loss) if est_loss is not None else None,
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_STOP_PLAN,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_winner_target_plan(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    cfg: PairedBinaryStrategyConfig,
    leg: str,
) -> None:
    dedup = f"winner_target_plan:{leg}"
    if not should_emit(state, dedup):
        return
    survivor_entry = state.yes_entry if leg == "yes" else state.no_entry
    planned = state.yes_planned_target if leg == "yes" else state.no_planned_target
    trigger = state.yes_target if leg == "yes" else state.no_target
    payload = base_payload(state, yes, no)
    payload.update(
        {
            "survivor_leg": leg,
            "survivor_entry": str(survivor_entry) if survivor_entry is not None else None,
            "target_price": str(trigger) if trigger is not None else None,
            "planned_target_price": str(planned) if planned is not None else None,
            "trigger_target_price": str(trigger) if trigger is not None else None,
            "profit_budget": str(state.profit_budget) if state.profit_budget is not None else None,
            "desired_net_profit_per_pair": (
                str(state.desired_net_profit_per_pair)
                if state.desired_net_profit_per_pair is not None
                else None
            ),
            "realized_loser_loss_if_known": None,
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_WINNER_TARGET_PLAN,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_winner_target_repriced(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    survivor_leg: str,
    realized_loser_loss: Decimal,
    required_winner_gain: Decimal,
    old_target: Decimal | None,
    new_target: Decimal | None,
) -> None:
    payload = base_payload(state, yes, no)
    payload.update(
        {
            "survivor_leg": survivor_leg,
            "realized_loser_loss": str(realized_loser_loss),
            "desired_net_profit": (
                str(state.desired_net_profit_per_pair)
                if state.desired_net_profit_per_pair is not None
                else None
            ),
            "required_winner_gain": str(required_winner_gain),
            "old_target": str(old_target) if old_target is not None else None,
            "new_target": str(new_target) if new_target is not None else None,
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_WINNER_TARGET_REPRICED,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_activation_rejected_loss_budget(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    reason: str,
) -> None:
    if not should_emit(state, f"activation_rejected:{reason}"):
        return
    yes_gap = (
        (state.yes_entry - yes.bid)
        if state.yes_entry is not None and yes.bid is not None
        else None
    )
    no_gap = (
        (state.no_entry - no.bid) if state.no_entry is not None and no.bid is not None else None
    )
    payload = base_payload(state, yes, no)
    payload.update(
        {
            "reason": reason,
            "loss_budget": str(state.loss_budget) if state.loss_budget is not None else None,
            "yes_immediate_exit_gap": str(yes_gap) if yes_gap is not None else None,
            "no_immediate_exit_gap": str(no_gap) if no_gap is not None else None,
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_ACTIVATION_REJECTED_LOSS_BUDGET,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def _cashflow_pnl_missing_fields(state: PairedBinaryRuntimeState) -> list[str]:
    missing: list[str] = []
    checks = (
        ("yes_entry_cash", state.yes.entry_cash),
        ("no_entry_cash", state.no.entry_cash),
        ("yes_exit_cash", state.yes.exit_cash),
        ("no_exit_cash", state.no.exit_cash),
        ("yes_entry_qty", state.yes.entry_qty),
        ("no_entry_qty", state.no.entry_qty),
        ("yes_exit_qty", state.yes.exit_qty),
        ("no_exit_qty", state.no.exit_qty),
    )
    for name, val in checks:
        if val is None:
            missing.append(name)
    return missing


def _available_cashflow_sources(state: PairedBinaryRuntimeState) -> dict[str, str | None]:
    return {
        "yes_entry_cash_source": state.yes.entry_cash_source,
        "no_entry_cash_source": state.no.entry_cash_source,
        "yes_exit_cash_source": state.yes.exit_cash_source,
        "no_exit_cash_source": state.no.exit_cash_source,
    }


def _price_estimate_exit_prices(state: PairedBinaryRuntimeState) -> tuple[Decimal | None, Decimal | None]:
    """Non-authoritative display/trigger prices for diagnostic estimate only."""
    yes_exit = leg_exit_avg_price(state.yes) or state.yes_exit or state.yes.last_exit_bid
    no_exit = leg_exit_avg_price(state.no) or state.no_exit or state.no.last_exit_bid
    return yes_exit, no_exit


def emit_realized_pnl_unavailable(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
) -> None:
    if not should_emit(state, "realized_pnl_unavailable"):
        return
    missing = _cashflow_pnl_missing_fields(state)
    if not missing:
        return
    payload = base_payload(state, yes, no)
    payload.update(
        {
            "missing_fields": missing,
            "available_sources": _available_cashflow_sources(state),
            "state_phase": state.phase.value,
            "pair_correlation_id": state.pair_correlation_id,
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_REALIZED_PNL_UNAVAILABLE,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_price_based_pnl_estimate(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
) -> None:
    if not should_emit(state, "price_based_pnl_estimate"):
        return
    yes_entry = leg_entry_avg_price(state.yes) or state.yes_entry
    no_entry = leg_entry_avg_price(state.no) or state.no_entry
    yes_exit, no_exit = _price_estimate_exit_prices(state)
    if yes_entry is None or no_entry is None:
        return
    result = price_based_pnl_estimate_from_stored_prices(
        yes_entry=yes_entry,
        no_entry=no_entry,
        yes_exit=yes_exit,
        no_exit=no_exit,
        qty=state.effective_qty,
    )
    if result is None:
        return
    pc, exit_value, pnl_per_pair, pnl_total = result
    payload = base_payload(state, yes, no)
    payload.update(
        {
            "authoritative": False,
            "yes_entry_avg_price": str(yes_entry),
            "no_entry_avg_price": str(no_entry),
            "yes_exit_avg_price": str(yes_exit) if yes_exit is not None else None,
            "no_exit_avg_price": str(no_exit) if no_exit is not None else None,
            "pair_entry_avg_cost": str(pc),
            "pair_exit_avg_value": str(exit_value),
            "pnl_per_pair": str(pnl_per_pair),
            "pnl_total": str(pnl_total),
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_PRICE_BASED_PNL_ESTIMATE,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_realized_pnl(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
) -> None:
    if not should_emit(state, "realized_pnl"):
        return
    result = realized_pnl_from_cashflows(state)
    if result is None:
        emit_realized_pnl_unavailable(sink, run_id, state, yes, no)
        emit_price_based_pnl_estimate(sink, run_id, state, yes, no)
        return
    yes_entry_avg = leg_entry_avg_price(state.yes)
    no_entry_avg = leg_entry_avg_price(state.no)
    yes_exit_avg = leg_exit_avg_price(state.yes)
    no_exit_avg = leg_exit_avg_price(state.no)
    pair_entry_avg_cost = None
    pair_exit_avg_value = None
    if yes_entry_avg is not None and no_entry_avg is not None:
        pair_entry_avg_cost = yes_entry_avg + no_entry_avg
    if yes_exit_avg is not None and no_exit_avg is not None:
        pair_exit_avg_value = yes_exit_avg + no_exit_avg
    expected = state.expected_pnl_total
    delta = result.pnl_total - expected if expected is not None else None
    payload = base_payload(state, yes, no)
    payload.update(
        {
            "yes_entry_cash": str(result.yes_entry_cash),
            "no_entry_cash": str(result.no_entry_cash),
            "yes_exit_cash": str(result.yes_exit_cash),
            "no_exit_cash": str(result.no_exit_cash),
            "yes_entry_qty": str(result.yes_entry_qty),
            "no_entry_qty": str(result.no_entry_qty),
            "yes_exit_qty": str(result.yes_exit_qty),
            "no_exit_qty": str(result.no_exit_qty),
            "yes_entry_avg_price": str(yes_entry_avg) if yes_entry_avg is not None else None,
            "no_entry_avg_price": str(no_entry_avg) if no_entry_avg is not None else None,
            "yes_exit_avg_price": str(yes_exit_avg) if yes_exit_avg is not None else None,
            "no_exit_avg_price": str(no_exit_avg) if no_exit_avg is not None else None,
            "pair_entry_avg_cost": str(pair_entry_avg_cost) if pair_entry_avg_cost is not None else None,
            "pair_exit_avg_value": str(pair_exit_avg_value) if pair_exit_avg_value is not None else None,
            "buy_cash_total": str(result.buy_cash_total),
            "sell_cash_total": str(result.sell_cash_total),
            "pnl_total": str(result.pnl_total),
            "pnl_per_pair": str(result.pnl_per_pair),
            "effective_pair_qty": str(result.effective_pair_qty),
            "yes_entry_cash_source": state.yes.entry_cash_source,
            "no_entry_cash_source": state.no.entry_cash_source,
            "yes_exit_cash_source": state.yes.exit_cash_source,
            "no_exit_cash_source": state.no.exit_cash_source,
            "expected_pnl_total": str(expected) if expected is not None else None,
            "pnl_delta_vs_plan": str(delta) if delta is not None else None,
        }
    )
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_REALIZED_PNL,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def _preflight_leg_payload(pf) -> dict[str, Any]:
    return {
        "leg": pf.leg,
        "yes_risk_approved" if pf.leg == "yes" else "no_risk_approved": pf.risk_approved,
        f"{pf.leg}_risk_approved": pf.risk_approved,
        f"{pf.leg}_planner_approved": pf.planner_approved,
        f"{pf.leg}_planned_valid": pf.planned_valid,
        f"{pf.leg}_notional": str(pf.notional_usd),
        f"{pf.leg}_requested_style": pf.requested_style,
        f"{pf.leg}_planned_style": pf.planned_style,
    }


def emit_pair_preflight(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    pair_correlation_id: str,
    yes_pf,
    no_pf,
    pair_notional: Decimal,
    approved: bool,
) -> None:
    payload = {
        **base_payload(state, yes, no),
        "pair_correlation_id": pair_correlation_id,
        "position_size": str(state.yes.target_qty or state.no.target_qty or ""),
        "yes_price": str(yes.ask) if yes.ask is not None else None,
        "no_price": str(no.ask) if no.ask is not None else None,
        "yes_notional": str(yes_pf.notional_usd),
        "no_notional": str(no_pf.notional_usd),
        "pair_notional": str(pair_notional),
        "yes_risk_approved": yes_pf.risk_approved,
        "no_risk_approved": no_pf.risk_approved,
        "yes_planner_approved": yes_pf.planner_approved,
        "no_planner_approved": no_pf.planner_approved,
        "yes_planned_valid": yes_pf.planned_valid,
        "no_planned_valid": no_pf.planned_valid,
        "approved": approved,
        "reason_codes": list(yes_pf.reason_codes) + list(no_pf.reason_codes),
    }
    _write_fact(
        sink,
        make_fact(FACT_TYPE_PAIRED_BINARY_PAIR_PREFLIGHT, str(run_id), payload),
    )


def emit_pair_preflight_rejected(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    pair_correlation_id: str,
    reason_codes: list[str],
) -> None:
    payload = {
        **base_payload(state, yes, no),
        "pair_correlation_id": pair_correlation_id,
        "reason_codes": reason_codes,
    }
    _write_fact(
        sink,
        make_fact(FACT_TYPE_PAIRED_BINARY_PAIR_PREFLIGHT_REJECTED, str(run_id), payload),
    )


def emit_entry_order_style_applied(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    leg: str,
    requested_style: str,
    planned_style: str,
    venue_style: str,
    planner_reason: str | None,
    style_mismatch: bool,
) -> None:
    payload = {
        **base_payload(state, yes, no),
        "leg": leg,
        "requested_style": requested_style,
        "planned_style": planned_style,
        "venue_style": venue_style,
        "planner_reason": planner_reason,
        "style_mismatch": style_mismatch,
    }
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_ENTRY_ORDER_STYLE_APPLIED,
            str(run_id),
            payload,
            correlation_id=f"{state.pair_correlation_id}:{leg}:entry",
        ),
    )


def emit_entry_leg_blocked(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    blocked_leg: str,
    blocking_phase: str,
    reason_codes: list[str],
    other_leg_status: str | None = None,
    action_taken: str | None = None,
) -> None:
    payload = {
        **base_payload(state, yes, no),
        "blocked_leg": blocked_leg,
        "blocking_phase": blocking_phase,
        "reason_codes": reason_codes,
        "other_leg_status": other_leg_status,
        "action_taken": action_taken,
    }
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_ENTRY_LEG_BLOCKED,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_entry_asymmetry_detected(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    blocked_leg: str,
    reason: str,
    yes_filled_qty: Decimal,
    no_filled_qty: Decimal,
    action_taken: str,
) -> None:
    payload = {
        **base_payload(state, yes, no),
        "blocked_leg": blocked_leg,
        "reason": reason,
        "yes_filled_qty": str(yes_filled_qty),
        "no_filled_qty": str(no_filled_qty),
        "other_leg_filled_qty": str(yes_filled_qty if blocked_leg == "no" else no_filled_qty),
        "action_taken": action_taken,
    }
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_ENTRY_ASYMMETRY_DETECTED,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_entry_cancel_attempt(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    leg: str,
    client_order_id: str,
    venue_order_id: str | None,
    reason: str,
    pair_correlation_id: str,
) -> None:
    payload = {
        **base_payload(state, yes, no),
        "leg": leg,
        "client_order_id": client_order_id,
        "order_id": venue_order_id,
        "reason": reason,
        "phase": state.pair_entry_saga_phase,
        "pair_correlation_id": pair_correlation_id,
    }
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_ENTRY_CANCEL_ATTEMPT,
            str(run_id),
            payload,
            correlation_id=f"{pair_correlation_id}:{leg}:cancel",
        ),
    )


def emit_entry_cancel_ack(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    leg: str,
    client_order_id: str,
    venue_order_id: str | None,
    reason: str,
    pair_correlation_id: str,
) -> None:
    payload = {
        **base_payload(state, yes, no),
        "leg": leg,
        "client_order_id": client_order_id,
        "order_id": venue_order_id,
        "reason": reason,
        "phase": state.pair_entry_saga_phase,
        "pair_correlation_id": pair_correlation_id,
    }
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_ENTRY_CANCEL_ACK,
            str(run_id),
            payload,
            correlation_id=f"{pair_correlation_id}:{leg}:cancel",
        ),
    )


def emit_entry_cancel_failed(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    leg: str,
    client_order_id: str,
    venue_order_id: str | None,
    reason: str,
    pair_correlation_id: str,
) -> None:
    payload = {
        **base_payload(state, yes, no),
        "leg": leg,
        "client_order_id": client_order_id,
        "order_id": venue_order_id,
        "reason": reason,
        "phase": state.pair_entry_saga_phase,
        "pair_correlation_id": pair_correlation_id,
    }
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_ENTRY_CANCEL_FAILED,
            str(run_id),
            payload,
            correlation_id=f"{pair_correlation_id}:{leg}:cancel",
        ),
    )


def emit_pair_entry_committed(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    pair_correlation_id: str,
) -> None:
    payload = {**base_payload(state, yes, no), "pair_correlation_id": pair_correlation_id}
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_PAIR_ENTRY_COMMITTED,
            str(run_id),
            payload,
            correlation_id=pair_correlation_id,
        ),
    )


def emit_pair_entry_aborted_flat(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    reason: str,
) -> None:
    payload = {**base_payload(state, yes, no), "reason": reason}
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_PAIR_ENTRY_ABORTED_FLAT,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )


def emit_pair_entry_manual_intervention(
    sink: JsonlSink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    yes: LegBook,
    no: LegBook,
    *,
    reason: str,
    attempt_count: int,
) -> None:
    payload = {**base_payload(state, yes, no), "reason": reason, "attempt_count": attempt_count}
    _write_fact(
        sink,
        make_fact(
            FACT_TYPE_PAIRED_BINARY_PAIR_ENTRY_MANUAL_INTERVENTION,
            str(run_id),
            payload,
            correlation_id=state.pair_correlation_id,
        ),
    )

