"""Z-Gap enforce runtime — entry activation and minimal exits (A0.7)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.execution.adapters import OMSBackend
from tyrex_pm.market_data.book_read import read_pair_books
from tyrex_pm.quant.edge import LEG_DOWN, LEG_UP
from tyrex_pm.runtime.allocation_runtime import fill_qty_for_allocation, maybe_release_exit_reservation
from tyrex_pm.runtime.config import (
    AppConfig,
    Z_GAP_ENTRY_MODE_ENFORCE,
    ZGapExitConfig,
    ZGapReconciliationConfig,
    ZGapStrategyConfig,
    _parse_z_gap_exit,
    _parse_z_gap_reconciliation,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.pipeline import process_intent_work_unit
from tyrex_pm.runtime.time_authority import SYNC_STATUS_SYNCED
from tyrex_pm.runtime.z_gap_live import z_gap_preflight_dir
from tyrex_pm.runtime.z_gap_preflight import load_z_gap_preflight_gates
from tyrex_pm.runtime.z_gap_run import (
    DEFAULT_TICK_INTERVAL_S,
    ObserveTickContext,
    ZGapStartupTimings,
    compute_tau_s,
    corrected_now_dt,
    corrected_now_ts,
    emit_startup_timing_fact,
    resolve_fee_model,
    run_observe_tick,
    sigma_config_from_zg,
)
from tyrex_pm.state.signal_state_store import FRESHNESS_FRESH
from tyrex_pm.strategies.z_gap import facts as zg_facts
from tyrex_pm.strategies.z_gap.entry_eval import DECISION_WOULD_ENTER
from tyrex_pm.strategies.z_gap.entry_plan import (
    build_z_gap_entry_plan,
    validate_z_gap_pre_submit,
    z_gap_entry_plan_to_intent_work_unit,
)
from tyrex_pm.strategies.z_gap.exit_eval import ExitTriggerDecision, ZGapExitEvalState, evaluate_exit_triggers
from tyrex_pm.strategies.z_gap.exit_plan import build_z_gap_exit_work_unit
from tyrex_pm.strategies.z_gap.facts import ZGapObserveRuntimeState
from tyrex_pm.strategies.z_gap.lifecycle import (
    allocated_quantity,
    mark_entry_submitted,
    mark_exit_submitted,
    mark_exit_triggered,
    reconcile_entry_fill,
    reconcile_exit_fill,
    resolve_entry_snapshot,
    verify_allocation_reconciled,
)
from tyrex_pm.strategies.z_gap.state import (
    ZGapLifecycleState,
    ZGapPhase,
    assert_startup_state_terminal_or_absent,
)
from tyrex_pm.strategies.z_gap.reconciliation import (
    RECON_MISMATCH,
    RECON_UNRESOLVED,
    RECON_VENUE_LAG_EXPECTED,
    ReconciliationTracker,
    evaluate_position_reconciliation,
    venue_reported_quantity,
)
from tyrex_pm.strategies.z_gap.strategy import ZGapStrategy
from tyrex_pm.quant.volatility import EwmaVolatilityEstimator

log = logging.getLogger(__name__)


def _manual_intervention_from_preflight() -> bool:
    import json

    path = z_gap_preflight_dir() / "operator_enforce_approval.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(isinstance(data, dict) and data.get("manual_intervention"))


@dataclass
class ZGapEnforceRuntimeState:
    observe: ZGapObserveRuntimeState = field(default_factory=ZGapObserveRuntimeState)
    lifecycle: ZGapLifecycleState | None = None
    exit_eval: ZGapExitEvalState = field(default_factory=ZGapExitEvalState)
    last_exit_attempt_mono: float | None = None
    pending_exit_reason: str | None = None
    books_cache: Any = None
    fair_cache: Any = None
    vol_cache: Any = None
    recon_tracker: ReconciliationTracker = field(default_factory=ReconciliationTracker)
    last_reconciliation_status: str | None = None


def _default_exit_cfg(zg: ZGapStrategyConfig) -> ZGapExitConfig:
    return zg.exit or _parse_z_gap_exit(None)


def _lifecycle_state_path() -> Path:
    base = z_gap_preflight_dir()
    return base / "lifecycle_state.json"


def _load_persisted_lifecycle(zg: ZGapStrategyConfig) -> None:
    path = _lifecycle_state_path()
    if not path.exists():
        return
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert_startup_state_terminal_or_absent(raw)
    if raw.get("phase") not in {ZGapPhase.DONE.value, ZGapPhase.FAILED.value}:
        raise RuntimeError(f"z_gap persisted lifecycle not terminal: {raw.get('phase')}")


def _persist_lifecycle(lifecycle: ZGapLifecycleState) -> None:
    path = _lifecycle_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lifecycle.to_dict(), indent=2), encoding="utf-8")


def _operational_pass_enforce(state: ZGapEnforceRuntimeState) -> bool:
    lc = state.lifecycle
    if lc is None:
        return False
    if lc.manual_intervention_required:
        return False
    if lc.phase == ZGapPhase.FAILED:
        return False
    if lc.active_quantity < 0:
        return False
    if lc.phase == ZGapPhase.EXIT_PENDING:
        return False
    if lc.entry_filled_shares > 0 and not lc.position_closed and lc.phase != ZGapPhase.DONE:
        return False
    return bool(state.observe.loop_ran)


async def _submit_entry_if_ready(
    *,
    ctx: ObserveTickContext,
    enforce_state: ZGapEnforceRuntimeState,
    strategy: ZGapStrategy,
    oms: OMSBackend,
    evaln: Any,
    books: Any,
    fair: Any,
    apply_local_shadow_fill: bool,
) -> None:
    zg = ctx.zg
    lc = enforce_state.lifecycle
    assert lc is not None
    if not lc.can_submit_entry() or evaln.decision_status != DECISION_WOULD_ENTER:
        return

    entry_cfg = ctx.entry_cfg
    assert entry_cfg is not None
    plan = build_z_gap_entry_plan(
        evaln=evaln,
        fair=fair,
        edge=ctx.state.last_edge,
        books=books,
        entry_cfg=entry_cfg,
        sizing_cfg=zg.sizing,
        fee_model=ctx.fee_model,
        market_id=zg.market_id,
        condition_id=zg.condition_id,
        yes_token_id=zg.yes_token_id,
        no_token_id=zg.no_token_id,
        entry_mode=Z_GAP_ENTRY_MODE_ENFORCE,
        tick_size=ctx.tick_size,
    )
    preflight = load_z_gap_preflight_gates(z_gap_preflight_dir())
    validation = validate_z_gap_pre_submit(
        plan,
        signal=ctx.state.last_signal,
        fee_model_status=ctx.state.fee_model_status or "unknown",
        time_authority=ctx.time_authority,
        entry_mode=Z_GAP_ENTRY_MODE_ENFORCE,
        preflight=preflight,
        sizing_cfg=zg.sizing,
    )
    if not validation.passed:
        zg_facts.emit_z_gap_entry_plan_fact(ctx.sink, ctx.run_id, plan)
        return

    corr = f"z_gap_entry:{zg.market_id}:{uuid.uuid4().hex[:8]}"
    wu = z_gap_entry_plan_to_intent_work_unit(
        plan,
        owner_id=zg.owner_id,
        validation=validation,
        correlation_id=corr,
    )
    if wu is None:
        return

    zg_facts.emit_z_gap_entry_submitted(ctx.sink, ctx.run_id, plan, correlation_id=corr)
    await process_intent_work_unit(
        wu,
        app=ctx.app,
        run_id=ctx.run_id,
        strategy=strategy,
        coord=ctx.coord,
        sink=ctx.sink,
        oms=oms,
        apply_local_shadow_fill=apply_local_shadow_fill,
    )
    order_id = strategy.last_entry_client_order_id or corr
    p_model = plan.p_L
    mark_entry_submitted(
        lc,
        order_id=order_id,
        requested_shares=plan.shares or Decimal("0"),
        selected_leg=plan.selected_leg or "",
        token_id=plan.token_id or "",
        model_p=p_model,
        entry_z=Decimal(str(fair.z)) if fair.z is not None else None,
        entry_edge=plan.predicted_edge_at_limit,
        now=corrected_now_dt(ctx.time_authority, ctx.now_ts),
    )
    _persist_lifecycle(lc)


async def _reconcile_entry_pending(
    *,
    ctx: ObserveTickContext,
    enforce_state: ZGapEnforceRuntimeState,
    apply_local_shadow_fill: bool,
) -> None:
    lc = enforce_state.lifecycle
    assert lc is not None
    if lc.phase != ZGapPhase.ENTRY_PENDING:
        return
    snap = resolve_entry_snapshot(ctx.coord, lc)
    if snap is None:
        return
    outcome = reconcile_entry_fill(lc, snap, coord=ctx.coord, now=corrected_now_dt(ctx.time_authority, ctx.now_ts))
    from tyrex_pm.strategies.z_gap.execution_outcomes import classify_entry_outcome

    entry_outcome = classify_entry_outcome(
        submitted=lc.entry_submitted,
        acknowledged=lc.entry_order_id is not None,
        requested_qty=lc.entry_requested_shares,
        filled_qty=snap.filled_qty,
        order_status=str(snap.status.value) if hasattr(snap.status, "value") else str(snap.status),
        client_order_id=lc.entry_order_id,
        unresolved=outcome.event == "entry_pending",
    )
    lc.entry_outcome_category = entry_outcome.category
    zg_facts.emit_z_gap_entry_outcome(ctx.sink, ctx.run_id, entry_outcome)
    if outcome.event == "entry_unfilled":
        zg_facts.emit_z_gap_entry_unfilled(ctx.sink, ctx.run_id, lc)
        lc.exited_this_window = True
    elif outcome.event == "entry_filled":
        zg_facts.emit_z_gap_position_activated(ctx.sink, ctx.run_id, lc)
    elif outcome.failure:
        zg_facts.emit_z_gap_lifecycle_state(ctx.sink, ctx.run_id, lc, event="entry_failed")
    _persist_lifecycle(lc)
    _ = apply_local_shadow_fill


async def _maybe_trigger_exit(
    *,
    ctx: ObserveTickContext,
    enforce_state: ZGapEnforceRuntimeState,
    exit_cfg: ZGapExitConfig,
    preflight_manual: bool,
) -> ExitTriggerDecision | None:
    lc = enforce_state.lifecycle
    assert lc is not None
    if lc.phase != ZGapPhase.ACTIVE:
        return None
    if (
        lc.active_quantity > 0
        and enforce_state.pending_exit_reason
        and not lc.exit_triggered
    ):
        mark_exit_triggered(
            lc,
            exit_reason=enforce_state.pending_exit_reason,
            now=corrected_now_dt(ctx.time_authority, ctx.now_ts),
        )
        _persist_lifecycle(lc)
        return ExitTriggerDecision(
            should_exit=True,
            exit_reason=enforce_state.pending_exit_reason,
            held_leg=lc.selected_leg,
        )
    now_ts = corrected_now_ts(ctx.time_authority, ctx.now_ts)
    signal = ctx.state.last_signal
    feeds_fresh = bool(
        signal
        and signal.binance_freshness == FRESHNESS_FRESH
        and signal.chainlink_freshness == FRESHNESS_FRESH
    )
    market_critical = bool(signal and signal.feed_reject_reason)
    decision = evaluate_exit_triggers(
        lifecycle=lc,
        fair=enforce_state.fair_cache,
        vol=enforce_state.vol_cache,
        exit_cfg=exit_cfg,
        eval_state=enforce_state.exit_eval,
        event_end_ts=ctx.zg.event_end_ts,
        time_authority=ctx.time_authority,
        now_ts=now_ts,
        feeds_fresh=feeds_fresh,
        manual_intervention=preflight_manual,
        market_data_critical=market_critical,
    )
    if not decision.should_exit:
        return None
    if lc.exit_triggered and lc.phase == ZGapPhase.EXIT_PENDING:
        return decision
    mark_exit_triggered(lc, exit_reason=decision.exit_reason or "unknown", now=corrected_now_dt(ctx.time_authority, ctx.now_ts))
    enforce_state.pending_exit_reason = decision.exit_reason
    zg_facts.emit_model_exit_triggered(ctx.sink, ctx.run_id, lc, decision, tau_s=compute_tau_s(ctx.zg.event_end_ts, now_ts))
    _persist_lifecycle(lc)
    return decision


def _default_recon_cfg(zg: ZGapStrategyConfig) -> ZGapReconciliationConfig:
    return zg.reconciliation or _parse_z_gap_reconciliation(None)


def _emit_reconciliation(
    *,
    ctx: ObserveTickContext,
    enforce_state: ZGapEnforceRuntimeState,
    recon_cfg: ZGapReconciliationConfig,
) -> None:
    lc = enforce_state.lifecycle
    if lc is None or lc.token_id is None or lc.active_quantity <= 0:
        return
    result = evaluate_position_reconciliation(
        ctx.coord,
        lc,
        cfg=recon_cfg,
        tracker=enforce_state.recon_tracker,
        now_mono=time.monotonic(),
    )
    enforce_state.last_reconciliation_status = result.status
    zg_facts.emit_z_gap_position_reconciliation(ctx.sink, ctx.run_id, result)
    if result.status == RECON_MISMATCH:
        zg_facts.emit_z_gap_reconciliation_failed(ctx.sink, ctx.run_id, result)
        lc.transition(ZGapPhase.FAILED, reason=result.reason or "reconciliation_mismatch", now=corrected_now_dt(ctx.time_authority, ctx.now_ts))
        _persist_lifecycle(lc)
    elif result.status == RECON_UNRESOLVED:
        zg_facts.emit_z_gap_reconciliation_failed(ctx.sink, ctx.run_id, result)
        lc.manual_intervention_required = True
        lc.transition(ZGapPhase.FAILED, reason=result.reason or "reconciliation_unresolved", now=corrected_now_dt(ctx.time_authority, ctx.now_ts))
        _persist_lifecycle(lc)
    elif result.status == RECON_VENUE_LAG_EXPECTED:
        zg_facts.emit_z_gap_reconciliation_warning(ctx.sink, ctx.run_id, result)


async def _submit_exit_if_ready(
    *,
    ctx: ObserveTickContext,
    enforce_state: ZGapEnforceRuntimeState,
    strategy: ZGapStrategy,
    oms: OMSBackend,
    exit_cfg: ZGapExitConfig,
    apply_local_shadow_fill: bool,
) -> None:
    lc = enforce_state.lifecycle
    assert lc is not None
    if lc.phase != ZGapPhase.EXIT_PENDING:
        return
    if lc.exit_order_id is not None:
        return

    now_mono = time.monotonic()
    if enforce_state.last_exit_attempt_mono is not None:
        elapsed_ms = (now_mono - enforce_state.last_exit_attempt_mono) * 1000.0
        if elapsed_ms < exit_cfg.retry_interval_ms:
            return

    if lc.exit_attempts >= exit_cfg.max_exit_attempts and lc.active_quantity > 0:
        lc.manual_intervention_required = True
        lc.transition(ZGapPhase.FAILED, reason="max_exit_attempts", now=corrected_now_dt(ctx.time_authority, ctx.now_ts))
        zg_facts.emit_z_gap_manual_intervention_required(ctx.sink, ctx.run_id, lc)
        _persist_lifecycle(lc)
        return

    books = enforce_state.books_cache
    if books is None:
        return
    reason = enforce_state.pending_exit_reason or lc.exit_reason or "exit"
    corr = f"z_gap_exit:{lc.market_id}:{uuid.uuid4().hex[:8]}"
    wu = build_z_gap_exit_work_unit(lc, books=books, coord=ctx.coord, exit_reason=reason, correlation_id=corr)
    if wu is None:
        return

    zg_facts.emit_z_gap_exit_attempt(ctx.sink, ctx.run_id, lc, attempt_number=lc.exit_attempts + 1)
    alloc_before = allocated_quantity(ctx.coord, owner_id=lc.owner_id, token_id=lc.token_id or "")
    prev_exit_order = strategy.last_exit_client_order_id
    await process_intent_work_unit(
        wu,
        app=ctx.app,
        run_id=ctx.run_id,
        strategy=strategy,
        coord=ctx.coord,
        sink=ctx.sink,
        oms=oms,
        apply_local_shadow_fill=apply_local_shadow_fill,
    )
    if strategy.last_exit_client_order_id is None:
        return
    if prev_exit_order is not None and strategy.last_exit_client_order_id == prev_exit_order:
        return
    order_id = strategy.last_exit_client_order_id
    mark_exit_submitted(lc, order_id=order_id, requested_shares=wu.intent.size)
    enforce_state.last_exit_attempt_mono = now_mono
    zg_facts.emit_z_gap_exit_submitted(ctx.sink, ctx.run_id, lc, correlation_id=corr)
    _persist_lifecycle(lc)

    alloc_after = allocated_quantity(ctx.coord, owner_id=lc.owner_id, token_id=lc.token_id or "")
    filled = alloc_before - alloc_after if alloc_before > alloc_after else Decimal("0")
    if filled <= 0 and order_id:
        from tyrex_pm.core.ids import ClientOrderId

        lo = ctx.coord.orders.orders.get(ClientOrderId(order_id))
        if lo is not None and lo.size_matched and lo.size_matched > 0:
            filled = lo.size_matched
    outcome = reconcile_exit_fill(
        lc,
        filled_qty=filled,
        coord=ctx.coord,
        now=corrected_now_dt(ctx.time_authority, ctx.now_ts),
    )
    if outcome.event == "exit_unfilled":
        zg_facts.emit_z_gap_exit_unfilled(ctx.sink, ctx.run_id, lc)
        from tyrex_pm.core.ids import ClientOrderId
        from tyrex_pm.execution.order_lifecycle import remove_resting_order

        remove_resting_order(ctx.coord.orders, ClientOrderId(order_id))
        maybe_release_exit_reservation(
            ctx.coord,
            ctx.app,
            client_order_id=order_id,
            correlation_id=corr,
            run_id=str(ctx.run_id),
        )
        strategy.last_exit_client_order_id = None
        lc.exit_triggered = False
    elif outcome.event in {"exit_partial", "exit_closed"}:
        zg_facts.emit_z_gap_exit_fill(ctx.sink, ctx.run_id, lc, filled_qty=filled, remaining=outcome.residual)
        if outcome.closed:
            zg_facts.emit_z_gap_position_closed(ctx.sink, ctx.run_id, lc)
    elif outcome.failure:
        zg_facts.emit_z_gap_lifecycle_state(ctx.sink, ctx.run_id, lc, event="exit_failed")
    if lc.exit_attempts >= exit_cfg.max_exit_attempts and lc.active_quantity > 0:
        lc.manual_intervention_required = True
        lc.transition(
            ZGapPhase.FAILED,
            reason="max_exit_attempts",
            now=corrected_now_dt(ctx.time_authority, ctx.now_ts),
        )
        zg_facts.emit_z_gap_manual_intervention_required(ctx.sink, ctx.run_id, lc)
    _persist_lifecycle(lc)


async def run_enforce_tick(
    *,
    ctx: ObserveTickContext,
    enforce_state: ZGapEnforceRuntimeState,
    strategy: ZGapStrategy,
    oms: OMSBackend,
    exit_cfg: ZGapExitConfig,
    apply_local_shadow_fill: bool = True,
    preflight_manual: bool = False,
) -> None:
    """One enforce tick: model eval, entry, reconciliation, exits."""
    if enforce_state.lifecycle is None:
        return

    evaln = run_observe_tick(ctx, allow_enforce=True)
    enforce_state.fair_cache = ctx.state.last_fair
    enforce_state.vol_cache = ctx.estimator.snapshot()
    now_dt = corrected_now_dt(ctx.time_authority, ctx.now_ts)
    enforce_state.books_cache = read_pair_books(
        ctx.coord.market_state,
        yes_token_id=ctx.zg.yes_token_id,
        no_token_id=ctx.zg.no_token_id,
        max_book_age_s=float(ctx.app.runtime.market_data.max_book_age_s),
        now=now_dt,
        time_authority=ctx.time_authority,
    )

    lc = enforce_state.lifecycle
    zg_facts.emit_z_gap_lifecycle_state(ctx.sink, ctx.run_id, lc)

    if lc.phase == ZGapPhase.IDLE and evaln is not None:
        await _submit_entry_if_ready(
            ctx=ctx,
            enforce_state=enforce_state,
            strategy=strategy,
            oms=oms,
            evaln=evaln,
            books=enforce_state.books_cache,
            fair=enforce_state.fair_cache,
            apply_local_shadow_fill=apply_local_shadow_fill,
        )

    await _reconcile_entry_pending(ctx=ctx, enforce_state=enforce_state, apply_local_shadow_fill=apply_local_shadow_fill)

    recon_cfg = _default_recon_cfg(ctx.zg)
    if lc.phase == ZGapPhase.ACTIVE and lc.active_quantity > 0:
        _emit_reconciliation(ctx=ctx, enforce_state=enforce_state, recon_cfg=recon_cfg)

    await _maybe_trigger_exit(
        ctx=ctx,
        enforce_state=enforce_state,
        exit_cfg=exit_cfg,
        preflight_manual=preflight_manual,
    )

    if lc.phase == ZGapPhase.EXIT_PENDING and lc.active_quantity > 0:
        await _submit_exit_if_ready(
            ctx=ctx,
            enforce_state=enforce_state,
            strategy=strategy,
            oms=oms,
            exit_cfg=exit_cfg,
            apply_local_shadow_fill=apply_local_shadow_fill,
        )


async def run_z_gap_enforce_loop(
    *,
    app: AppConfig,
    run_id: RunId,
    coord: RuntimeCoordinator,
    sink: Any,
    strategy: ZGapStrategy,
    oms: OMSBackend,
    stop: asyncio.Event | None = None,
    tick_interval_s: float = DEFAULT_TICK_INTERVAL_S,
    max_ticks: int | None = None,
    apply_local_shadow_fill: bool = True,
    time_authority: Any = None,
    startup_timings: ZGapStartupTimings | None = None,
    fd_raw: dict[str, Any] | None = None,
    estimator: EwmaVolatilityEstimator | None = None,
) -> int:
    """Enforce loop for Z-Gap Phase A2 — no hold-to-resolution."""
    assert app.z_gap is not None
    zg = app.z_gap
    if zg.entry_mode != Z_GAP_ENTRY_MODE_ENFORCE:
        log.error("z_gap enforce loop refused: entry_mode=%s", zg.entry_mode)
        return 1

    _load_persisted_lifecycle(zg)
    exit_cfg = _default_exit_cfg(zg)
    enforce_state = ZGapEnforceRuntimeState(
        lifecycle=ZGapLifecycleState(
            market_id=zg.market_id,
            condition_id=zg.condition_id,
            owner_id=zg.owner_id,
        ),
    )

    estimator = estimator or EwmaVolatilityEstimator(config=sigma_config_from_zg(zg))
    fee_model = await resolve_fee_model(coord, zg, fd_raw=fd_raw)
    ta = time_authority or coord.time_authority
    entry_cfg = zg.entry

    if startup_timings is not None:
        emit_startup_timing_fact(sink, run_id, startup_timings)

    ticks = 0
    preflight = load_z_gap_preflight_gates(z_gap_preflight_dir())
    preflight_manual = _manual_intervention_from_preflight()

    while True:
        if stop is not None and stop.is_set():
            break
        if max_ticks is not None and ticks >= max_ticks:
            break
        lc = enforce_state.lifecycle
        if lc is not None and lc.is_terminal():
            break
        if zg.event_end_ts is not None and corrected_now_ts(ta) >= float(zg.event_end_ts):
            if lc is not None and lc.phase == ZGapPhase.ACTIVE and lc.active_quantity > 0:
                await _maybe_trigger_exit(
                    ctx=ObserveTickContext(
                        app=app,
                        zg=zg,
                        run_id=run_id,
                        coord=coord,
                        sink=sink,
                        state=enforce_state.observe,
                        estimator=estimator,
                        fee_model=fee_model,
                        time_authority=ta,
                        entry_cfg=entry_cfg,
                        lifecycle=lc,
                    ),
                    enforce_state=enforce_state,
                    exit_cfg=exit_cfg,
                    preflight_manual=preflight_manual,
                )
            break

        ctx = ObserveTickContext(
            app=app,
            zg=zg,
            run_id=run_id,
            coord=coord,
            sink=sink,
            state=enforce_state.observe,
            estimator=estimator,
            fee_model=fee_model,
            time_authority=ta,
            entry_cfg=entry_cfg,
            lifecycle=lc,
        )
        await run_enforce_tick(
            ctx=ctx,
            enforce_state=enforce_state,
            strategy=strategy,
            oms=oms,
            exit_cfg=exit_cfg,
            apply_local_shadow_fill=apply_local_shadow_fill,
            preflight_manual=preflight_manual,
        )
        ticks += 1
        if stop is not None and stop.is_set():
            break
        if max_ticks is not None and ticks >= max_ticks:
            break
        if lc is not None and lc.is_terminal():
            break
        await asyncio.sleep(tick_interval_s)

    lc = enforce_state.lifecycle
    alloc_zero = True
    venue_qty = Decimal("0")
    alloc_qty = Decimal("0")
    if lc is not None and lc.token_id:
        alloc_qty = allocated_quantity(coord, owner_id=lc.owner_id, token_id=lc.token_id)
        alloc_zero = alloc_qty == 0
        venue_qty = venue_reported_quantity(coord, lc.token_id)

    zg_facts.emit_z_gap_enforce_terminal_summary(
        sink,
        run_id,
        enforce_state.observe,
        lifecycle=lc,
        entry_mode=Z_GAP_ENTRY_MODE_ENFORCE,
        market_id=zg.market_id,
        condition_id=zg.condition_id,
        signal=enforce_state.observe.last_signal,
        operational_pass=_operational_pass_enforce(enforce_state),
        allocation_zero=alloc_zero,
        venue_reported_quantity=venue_qty,
        allocated_quantity=alloc_qty,
        reconciliation_status=enforce_state.last_reconciliation_status,
        requested_entry_quantity=lc.entry_requested_shares if lc else Decimal("0"),
        filled_entry_quantity=lc.entry_filled_shares if lc else Decimal("0"),
        entry_attempts=1 if lc and lc.entry_attempted else 0,
    )
    if lc is not None:
        _persist_lifecycle(lc)
    return 0 if _operational_pass_enforce(enforce_state) else 1
