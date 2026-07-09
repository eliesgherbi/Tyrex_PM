"""Paired-binary open-exposure shutdown policy (Phase 2 operational hardening)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Awaitable, Callable

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.market_data.quality import DecisionContext
from tyrex_pm.runtime.config import (
    AppConfig,
    OPEN_EXPOSURE_ON_MAX_RUNTIME_CONTINUE,
    OPEN_EXPOSURE_ON_MAX_RUNTIME_FORCE,
    OPEN_EXPOSURE_ON_MAX_RUNTIME_MANUAL,
    PairedBinaryRuntimeConfig,
    PairedBinaryStrategyConfig,
    SURVIVOR_ON_MAX_RUNTIME_CONTINUE,
    _normalize_open_exposure_policy,
)
from tyrex_pm.strategies.paired_binary.activation_flow import leg_inventory_qty
from tyrex_pm.strategies.paired_binary.emergency_unwind import UnwindLegResult
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook
from tyrex_pm.strategies.paired_binary.lifecycle import transition_phase
from tyrex_pm.strategies.paired_binary.observability import emit_material_decision
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from tyrex_pm.strategies.paired_binary import facts as pb_facts
from tyrex_pm.runtime.pair_entry_saga import cancel_resting_entry_leg
from tyrex_pm.runtime.pipeline import process_intent_work_unit
from tyrex_pm.runtime.market_data_runtime import inject_fixture_book
from tyrex_pm.strategies.paired_binary.strategy import PairedBinaryStrategy
from tyrex_pm.strategies.paired_binary.entry_eval import read_leg_book

OPEN_EXPOSURE_PHASES = frozenset(
    {
        PairedBinaryPhase.BOTH_ENTRY_PENDING,
        PairedBinaryPhase.YES_ENTRY_PENDING,
        PairedBinaryPhase.NO_ENTRY_PENDING,
        PairedBinaryPhase.BOTH_LEGS_FILLED,
        PairedBinaryPhase.BOTH_LEGS_ACTIVE,
        PairedBinaryPhase.ONLY_YES_ACTIVE,
        PairedBinaryPhase.ONLY_NO_ACTIVE,
    }
)

SHUTDOWN_FLATTEN_REASON = "shutdown_flatten"
MARKET_CLOSE_FLATTEN_REASON = "market_close_flatten"


def _refresh_books_for_shutdown(
    coord,
    cfg: PairedBinaryStrategyConfig,
) -> tuple[LegBook, LegBook]:
    """Ensure books are fresh enough for URGENT_EXIT planner/risk on shutdown flatten."""
    if cfg.use_fixture_book:
        if cfg.fixture_yes_bid is None or cfg.fixture_yes_ask is None:
            raise RuntimeError("paired_binary fixture requires fixture_yes_bid/ask")
        if cfg.fixture_no_bid is None or cfg.fixture_no_ask is None:
            raise RuntimeError("paired_binary fixture requires fixture_no_bid/ask")
        inject_fixture_book(
            coord,
            cfg.yes_token_id,
            best_bid=cfg.fixture_yes_bid,
            best_ask=cfg.fixture_yes_ask,
        )
        inject_fixture_book(
            coord,
            cfg.no_token_id,
            best_bid=cfg.fixture_no_bid,
            best_ask=cfg.fixture_no_ask,
        )
    yes = read_leg_book(coord.market_state, TokenId(cfg.yes_token_id), max_book_age_s=cfg.max_book_age_s)
    no = read_leg_book(coord.market_state, TokenId(cfg.no_token_id), max_book_age_s=cfg.max_book_age_s)
    if yes is None or no is None:
        raise RuntimeError("shutdown flatten requires both leg books")
    return yes, no


def effective_open_exposure_policy(pb_rt: PairedBinaryRuntimeConfig) -> str:
    """``open_exposure_on_max_runtime`` wins; legacy survivor keys are normalized."""
    return _normalize_open_exposure_policy(pb_rt.open_exposure_on_max_runtime)


def effective_open_exposure_timeout_s(pb_rt: PairedBinaryRuntimeConfig) -> float:
    return pb_rt.open_exposure_timeout_s


def phase_has_open_exposure(phase: PairedBinaryPhase) -> bool:
    return phase in OPEN_EXPOSURE_PHASES


def _open_legs(yes_qty: Decimal, no_qty: Decimal) -> list[str]:
    legs: list[str] = []
    if yes_qty > 0:
        legs.append("yes")
    if no_qty > 0:
        legs.append("no")
    return legs


def _exposure_snapshot(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
    coord,
    yes_book: LegBook,
    no_book: LegBook,
    *,
    configured_policy: str,
    decision_id: str | None = None,
    shutdown_reason: str = "max_runtime_open_exposure",
) -> dict[str, object]:
    yes_qty = leg_inventory_qty(coord, cfg, "yes")
    no_qty = leg_inventory_qty(coord, cfg, "no")
    open_legs = _open_legs(yes_qty, no_qty)
    est_yes = (yes_qty * yes_book.bid) if yes_book.bid is not None else None
    est_no = (no_qty * no_book.bid) if no_book.bid is not None else None
    return {
        "state": state.phase.value,
        "open_legs": open_legs,
        "yes_qty": str(yes_qty),
        "no_qty": str(no_qty),
        "yes_token_id": cfg.yes_token_id,
        "no_token_id": cfg.no_token_id,
        "last_yes_bid": str(yes_book.bid) if yes_book.bid is not None else None,
        "last_no_bid": str(no_book.bid) if no_book.bid is not None else None,
        "estimated_yes_notional": str(est_yes) if est_yes is not None else None,
        "estimated_no_notional": str(est_no) if est_no is not None else None,
        "configured_policy": configured_policy,
        "reason": shutdown_reason,
        "decision_id": decision_id,
    }


def build_open_exposure_snapshot(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
    coord,
    yes_book: LegBook,
    no_book: LegBook,
    *,
    configured_policy: str,
    shutdown_reason: str = "max_runtime_open_exposure",
    decision_id: str | None = None,
) -> dict[str, object]:
    return _exposure_snapshot(
        state,
        cfg,
        coord,
        yes_book,
        no_book,
        configured_policy=configured_policy,
        decision_id=decision_id,
        shutdown_reason=shutdown_reason,
    )


@dataclass(frozen=True)
class ShutdownHandleResult:
    stop: bool
    continue_loop: bool = False
    extension_started: bool = False


UnwindLegFn = Callable[..., Awaitable[UnwindLegResult]]


async def _cancel_open_entry_legs(
    *,
    app: AppConfig,
    run_id: RunId,
    coord,
    sink,
    oms,
    strategy: PairedBinaryStrategy,
    cfg: PairedBinaryStrategyConfig,
    state: PairedBinaryRuntimeState,
    yes_book: LegBook,
    no_book: LegBook,
    apply_local_shadow_fill: bool,
    live_clob_client: object | None,
) -> None:
    if state.phase not in {
        PairedBinaryPhase.BOTH_ENTRY_PENDING,
        PairedBinaryPhase.YES_ENTRY_PENDING,
        PairedBinaryPhase.NO_ENTRY_PENDING,
    }:
        return
    pair_id = state.pair_correlation_id or "paired_binary_shutdown"
    for leg in ("yes", "no"):
        await cancel_resting_entry_leg(
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            oms=oms,
            strategy=strategy,
            cfg=cfg,
            state=state,
            yes_book=yes_book,
            no_book=no_book,
            leg=leg,
            reason="max_runtime_open_exposure",
            apply_local_shadow_fill=apply_local_shadow_fill,
            live_clob_client=live_clob_client,
            pair_correlation_id=pair_id,
        )


async def _cancel_survival_resting_exit_if_any(
    *,
    app: AppConfig,
    run_id: RunId,
    coord,
    sink,
    oms,
    strategy: PairedBinaryStrategy,
    cfg: PairedBinaryStrategyConfig,
    state: PairedBinaryRuntimeState,
    yes_book: LegBook,
    no_book: LegBook,
    apply_local_shadow_fill: bool,
    live_clob_client: object | None,
) -> None:
    from tyrex_pm.survival.enforcement_dispatch import (
        build_survival_shutdown_resting_cancel,
        mark_resting_cancelled,
    )

    cancel_work = build_survival_shutdown_resting_cancel(state, cfg, reason="terminal_shutdown")
    if cancel_work is None:
        return
    leg = str((cancel_work.intent_fact_extensions or {}).get("leg", "yes"))
    cancel_oid = str((cancel_work.intent_fact_extensions or {}).get("cancel_order_id") or "")
    pb_facts.emit_survival_exit_resting_order_cancel_requested(
        sink,
        run_id,
        state,
        yes_book,
        no_book,
        payload={
            "module": (state.survivor_leg_state or {}).get("enforce_module", "trailing_stop"),
            "survivor_leg": leg,
            "order_id": cancel_oid,
            "cancel_order_id": cancel_oid,
            "reason": "terminal_shutdown",
        },
    )
    await process_intent_work_unit(
        cancel_work,
        app=app,
        run_id=run_id,
        strategy=strategy,
        coord=coord,
        sink=sink,
        oms=oms,
        apply_local_shadow_fill=apply_local_shadow_fill,
        live_clob_client=live_clob_client,
    )
    mark_resting_cancelled(state)
    pb_facts.emit_survival_exit_resting_order_cancelled(
        sink,
        run_id,
        state,
        yes_book,
        no_book,
        payload={
            "module": (state.survivor_leg_state or {}).get("enforce_module", "trailing_stop"),
            "survivor_leg": leg,
            "order_id": cancel_oid,
            "cancel_order_id": cancel_oid,
            "reason": "terminal_shutdown",
        },
    )


async def _force_flatten_open_legs(
    *,
    app: AppConfig,
    run_id: RunId,
    coord,
    sink,
    oms,
    strategy: PairedBinaryStrategy,
    cfg: PairedBinaryStrategyConfig,
    state: PairedBinaryRuntimeState,
    yes_book: LegBook,
    no_book: LegBook,
    apply_local_shadow_fill: bool,
    live_clob_client: object | None,
    unwind_leg_fn: UnwindLegFn,
    decision_id: str | None,
    configured_policy: str,
) -> tuple[bool, list[str]]:
    await _cancel_survival_resting_exit_if_any(
        app=app,
        run_id=run_id,
        coord=coord,
        sink=sink,
        oms=oms,
        strategy=strategy,
        cfg=cfg,
        state=state,
        yes_book=yes_book,
        no_book=no_book,
        apply_local_shadow_fill=apply_local_shadow_fill,
        live_clob_client=live_clob_client,
    )
    await _cancel_open_entry_legs(
        app=app,
        run_id=run_id,
        coord=coord,
        sink=sink,
        oms=oms,
        strategy=strategy,
        cfg=cfg,
        state=state,
        yes_book=yes_book,
        no_book=no_book,
        apply_local_shadow_fill=apply_local_shadow_fill,
        live_clob_client=live_clob_client,
    )

    failures: list[str] = []
    for leg in ("yes", "no"):
        qty = leg_inventory_qty(coord, cfg, leg)
        if qty <= 0:
            continue
        token_id = TokenId(cfg.yes_token_id if leg == "yes" else cfg.no_token_id)
        book = yes_book if leg == "yes" else no_book
        result = await unwind_leg_fn(
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            oms=oms,
            strategy=strategy,
            cfg=cfg,
            state=state,
            leg=leg,
            token_id=token_id,
            qty=qty,
            reason=SHUTDOWN_FLATTEN_REASON,
            apply_local_shadow_fill=apply_local_shadow_fill,
            live_clob_client=live_clob_client,
            book=book,
            decision_id=decision_id,
        )
        if not result.submitted or result.blocked:
            reason = result.risk_reason or "force_flatten_not_submitted"
            if reason == "unknown":
                reason = "planner_or_submit_denied"
            failures.append(f"{leg}:{reason}")
            pb_facts.emit_venue_reduce_only_reject_if_applicable(
                sink,
                run_id,
                state,
                yes_book,
                no_book,
                leg=leg,
                failure_reason=reason,
                decision_id=decision_id,
            )

    yes_q = leg_inventory_qty(coord, cfg, "yes")
    no_q = leg_inventory_qty(coord, cfg, "no")
    flat = yes_q <= 0 and no_q <= 0
    snap = _exposure_snapshot(
        state, cfg, coord, yes_book, no_book, configured_policy=configured_policy, decision_id=decision_id
    )
    if flat:
        pb_facts.emit_shutdown_force_flatten_done(
            sink, run_id, state, yes_book, no_book, snapshot=snap
        )
        transition_phase(state, PairedBinaryPhase.DONE, reason="shutdown_force_flatten")
        yes_book, no_book = _refresh_books_for_shutdown(coord, cfg)
        pb_facts.emit_shutdown_completion_reporting(
            sink,
            run_id,
            state,
            yes_book,
            no_book,
            coord=coord,
            fill_reconciliation_cfg=app.execution.fill_reconciliation,
            decision_id=decision_id,
        )
    else:
        detail = ";".join(failures) if failures else "residual_exposure"
        pb_facts.emit_shutdown_force_flatten_failed(
            sink,
            run_id,
            state,
            yes_book,
            no_book,
            snapshot=snap,
            failure_reason=detail,
        )
        pb_facts.emit_manual_intervention_required(
            sink,
            run_id,
            state,
            yes_book,
            no_book,
            attempt_count=0,
            reason="max_runtime_open_exposure",
            extra={**snap, "failure_reason": detail},
        )
        transition_phase(state, PairedBinaryPhase.FAILED, reason="shutdown_force_flatten_failed")
    return flat, failures


async def handle_open_exposure_at_shutdown(
    *,
    app: AppConfig,
    run_id: RunId,
    coord,
    sink,
    oms,
    strategy: PairedBinaryStrategy,
    cfg: PairedBinaryStrategyConfig,
    state: PairedBinaryRuntimeState,
    yes_book: LegBook,
    no_book: LegBook,
    apply_local_shadow_fill: bool,
    live_clob_client: object | None,
    unwind_leg_fn: UnwindLegFn,
    emit_open_exposure_fact: bool = True,
    extension_elapsed: bool = False,
    shutdown_reason: str = "max_runtime_open_exposure",
) -> ShutdownHandleResult:
    pb_rt = app.runtime.paired_binary
    policy = effective_open_exposure_policy(pb_rt)
    snap = _exposure_snapshot(
        state, cfg, coord, yes_book, no_book, configured_policy=policy, shutdown_reason=shutdown_reason
    )

    if emit_open_exposure_fact:
        pb_facts.emit_open_exposure_at_shutdown(
            sink, run_id, state, yes_book, no_book, snapshot=snap
        )

    if policy == OPEN_EXPOSURE_ON_MAX_RUNTIME_MANUAL:
        pb_facts.emit_manual_intervention_required(
            sink,
            run_id,
            state,
            yes_book,
            no_book,
            attempt_count=0,
            reason="max_runtime_open_exposure",
            extra=snap,
        )
        transition_phase(state, PairedBinaryPhase.FAILED, reason="max_runtime_open_exposure")
        return ShutdownHandleResult(stop=True)

    if policy == OPEN_EXPOSURE_ON_MAX_RUNTIME_CONTINUE and not extension_elapsed:
        pb_facts.emit_shutdown_runtime_extension(
            sink,
            run_id,
            state,
            yes_book,
            no_book,
            snapshot=snap,
            open_exposure_timeout_s=effective_open_exposure_timeout_s(pb_rt),
        )
        return ShutdownHandleResult(stop=False, continue_loop=True, extension_started=True)

    if policy == OPEN_EXPOSURE_ON_MAX_RUNTIME_CONTINUE and extension_elapsed:
        pb_facts.emit_manual_intervention_required(
            sink,
            run_id,
            state,
            yes_book,
            no_book,
            attempt_count=0,
            reason="open_exposure_timeout_elapsed",
            extra=snap,
        )
        transition_phase(state, PairedBinaryPhase.FAILED, reason="open_exposure_timeout_elapsed")
        return ShutdownHandleResult(stop=True)

    yes_book, no_book = _refresh_books_for_shutdown(coord, cfg)
    total_qty = leg_inventory_qty(coord, cfg, "yes") + leg_inventory_qty(coord, cfg, "no")
    decision_id = emit_material_decision(
        app=app,
        coord=coord,
        sink=sink,
        run_id=run_id,
        cfg=cfg,
        state=state,
        decision_type="urgent_exit",
        context=DecisionContext.URGENT_EXIT,
        size=total_qty if total_qty > 0 else cfg.position_size,
        emit_latency=True,
    )
    snap = _exposure_snapshot(
        state, cfg, coord, yes_book, no_book, configured_policy=policy, decision_id=decision_id, shutdown_reason=shutdown_reason
    )
    pb_facts.emit_shutdown_force_flatten_started(
        sink, run_id, state, yes_book, no_book, snapshot=snap
    )

    await _force_flatten_open_legs(
        app=app,
        run_id=run_id,
        coord=coord,
        sink=sink,
        oms=oms,
        strategy=strategy,
        cfg=cfg,
        state=state,
        yes_book=yes_book,
        no_book=no_book,
        apply_local_shadow_fill=apply_local_shadow_fill,
        live_clob_client=live_clob_client,
        unwind_leg_fn=unwind_leg_fn,
        decision_id=decision_id,
        configured_policy=policy,
    )
    return ShutdownHandleResult(stop=True)
