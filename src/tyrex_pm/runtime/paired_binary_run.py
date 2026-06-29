"""Paired binary production loop (Phase 4.6)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from decimal import Decimal
from pathlib import Path

from tyrex_pm.core.enums import ExecutionMode, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import EnterIntent
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_HEALTH
from tyrex_pm.runtime.config import AppConfig, PairedBinaryStrategyConfig
from tyrex_pm.runtime.entry_qty_reconcile import PairEntryQtyReconcile, reconcile_pair_entry_qty
from tyrex_pm.runtime.intent_work import IntentWorkUnit
from tyrex_pm.runtime.market_data_runtime import bootstrap_market_state, inject_fixture_book
from tyrex_pm.runtime.pair_entry_saga import (
    abort_pair_entry,
    run_pair_entry_from_idle,
    tick_pair_entry_pending,
    _leg_filled_qty,
)
from tyrex_pm.runtime.pipeline import process_intent_work_unit
from tyrex_pm.strategies.base import StrategyContext
from tyrex_pm.strategies.paired_binary import facts as pb_facts
from tyrex_pm.strategies.paired_binary.entry_eval import (
    EntryEvalInput,
    LegBook,
    evaluate_entry,
    read_leg_book,
)
from tyrex_pm.strategies.paired_binary.lifecycle import (
    activate_monitoring,
    mark_both_legs_filled,
    resolve_min_effective_pair_qty,
    transition_phase,
)
from tyrex_pm.strategies.paired_binary.exit_engine import (
    both_legs_sellable,
    confirm_exit_submitted,
    reprice_survivor_after_loser_exit,
)
from tyrex_pm.strategies.paired_binary.activation_flow import (
    activation_recheck_elapsed_s,
    activation_recheck_expired,
    begin_activation_recheck,
    evaluate_activation_safety,
    leg_inventory_qty,
    should_use_activation_recheck,
)
from tyrex_pm.strategies.paired_binary.emergency_unwind import (
    EmergencyUnwindOutcome,
    UnwindLegResult,
    run_emergency_unwind_with_retry,
)
from tyrex_pm.strategies.paired_binary.entry_price import (
    detect_entry_price_mismatch,
    max_entry_price_mismatch,
    resolve_pair_entry_prices,
)
from tyrex_pm.strategies.paired_binary.latency import LatencyTracker
from tyrex_pm.strategies.paired_binary.monitor import PairedBinaryMonitor
from tyrex_pm.strategies.paired_binary.sizing import build_exit_work_unit, clamp_exit_size
from tyrex_pm.strategies.paired_binary.state import (
    PairedBinaryPhase,
    PairedBinaryRuntimeState,
    persistence_path,
    save_persisted_state,
)
from tyrex_pm.strategies.paired_binary.strategy import PairedBinaryStrategy

log = logging.getLogger(__name__)


def _entry_eval_input(cfg: PairedBinaryStrategyConfig, yes_book: LegBook, no_book: LegBook) -> EntryEvalInput:
    return EntryEvalInput(
        yes=yes_book,
        no=no_book,
        max_pair_entry_cost=cfg.max_pair_entry_cost,
        max_spread_yes=cfg.max_spread_yes,
        max_spread_no=cfg.max_spread_no,
        pair_stop_loss_pct=cfg.pair_stop_loss_pct,
        slippage_buffer=cfg.slippage_buffer,
        reject_if_spread_exceeds_loss_budget=cfg.reject_if_spread_exceeds_loss_budget,
    )


def _inject_fixture_books(coord, cfg: PairedBinaryStrategyConfig) -> None:
    if not cfg.use_fixture_book:
        return
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


def _books(coord, cfg: PairedBinaryStrategyConfig) -> tuple[LegBook, LegBook]:
    yes = read_leg_book(
        coord.market_state, TokenId(cfg.yes_token_id), max_book_age_s=cfg.max_book_age_s
    )
    no = read_leg_book(
        coord.market_state, TokenId(cfg.no_token_id), max_book_age_s=cfg.max_book_age_s
    )
    return yes, no


async def _submit_entry_intents(
    *,
    app: AppConfig,
    run_id: RunId,
    coord,
    sink,
    oms,
    strategy: PairedBinaryStrategy,
    cfg: PairedBinaryStrategyConfig,
    state: PairedBinaryRuntimeState,
    pair_correlation_id: str,
    apply_local_shadow_fill: bool,
    live_clob_client,
) -> None:
    ctx = StrategyContext(coord=coord, market_state=coord.market_state)
    _, skip = strategy.evaluate_entry(ctx, pair_correlation_id=pair_correlation_id)
    yes_book, no_book = _books(coord, cfg)
    if skip:
        eval_inp = _entry_eval_input(cfg, yes_book, no_book)
        ev = evaluate_entry(eval_inp)
        pb_facts.emit_entry_skip(
            sink,
            run_id,
            state,
            yes_book,
            no_book,
            reason=skip,
            pair_cost=ev.pair_cost,
            yes_spread=ev.yes_spread,
            no_spread=ev.no_spread,
            estimated_loss_budget=ev.estimated_loss_budget,
            slippage_buffer=ev.slippage_buffer,
        )
        return

    eval_inp = _entry_eval_input(cfg, yes_book, no_book)
    ev = evaluate_entry(eval_inp)
    if ev.pair_cost is None or ev.yes_spread is None or ev.no_spread is None:
        return
    pb_facts.emit_entry_eval(
        sink,
        run_id,
        state,
        yes_book,
        no_book,
        pair_cost=ev.pair_cost,
        yes_spread=ev.yes_spread,
        no_spread=ev.no_spread,
        estimated_loss_budget=ev.estimated_loss_budget,
        slippage_buffer=ev.slippage_buffer,
    )

    await run_pair_entry_from_idle(
        app=app,
        run_id=run_id,
        coord=coord,
        sink=sink,
        oms=oms,
        strategy=strategy,
        cfg=cfg,
        state=state,
        pair_correlation_id=pair_correlation_id,
        yes_book=yes_book,
        no_book=no_book,
        apply_local_shadow_fill=apply_local_shadow_fill,
        live_clob_client=live_clob_client,
        unwind_fn=_run_emergency_unwind,
    )


async def _apply_entry_prices_from_fills(
    *,
    coord,
    cfg: PairedBinaryStrategyConfig,
    state: PairedBinaryRuntimeState,
    pair: PairEntryQtyReconcile,
    sink,
    run_id: RunId,
    yes_book: LegBook,
    no_book: LegBook,
    apply_local_shadow_fill: bool,
) -> bool:
    """Resolve true fill prices. Returns False when prices are not yet available."""
    prices = resolve_pair_entry_prices(
        coord,
        yes_token_id=cfg.yes_token_id,
        no_token_id=cfg.no_token_id,
        yes_client_order_id=state.yes.entry_client_order_id,
        no_client_order_id=state.no.entry_client_order_id,
        apply_shadow_fill=apply_local_shadow_fill,
        state=state,
    )
    if not prices.ready:
        if prices.yes.source == "unknown":
            pb_facts.emit_entry_price_unknown(
                sink, run_id, state, yes_book, no_book, leg="yes"
            )
        if prices.no.source == "unknown":
            pb_facts.emit_entry_price_unknown(
                sink, run_id, state, yes_book, no_book, leg="no"
            )
        return False

    for leg_name, leg_res in (("yes", prices.yes), ("no", prices.no)):
        mismatch = detect_entry_price_mismatch(
            leg_name, leg_res, tolerance=cfg.entry_price_mismatch_tolerance
        )
        if mismatch is not None:
            pb_facts.emit_entry_price_mismatch(
                sink, run_id, state, yes_book, no_book, mismatch=mismatch
            )

    abort_thr = cfg.entry_price_mismatch_abort_threshold
    if abort_thr is not None and max_entry_price_mismatch(prices) > abort_thr:
        return False

    return True


async def _reconcile_pair_entry(
    *,
    app: AppConfig,
    run_id: RunId,
    coord,
    cfg: PairedBinaryStrategyConfig,
    state: PairedBinaryRuntimeState,
    repair: bool = True,
) -> PairEntryQtyReconcile:
    return reconcile_pair_entry_qty(
        coord,
        app,
        owner_id=cfg.owner_id,
        yes_token_id=cfg.yes_token_id,
        no_token_id=cfg.no_token_id,
        pair_correlation_id=state.pair_correlation_id or "",
        yes_leg_correlation_id=state.yes.leg_correlation_id,
        no_leg_correlation_id=state.no.leg_correlation_id,
        yes_client_order_id=state.yes.entry_client_order_id,
        no_client_order_id=state.no.entry_client_order_id,
        run_id=run_id,
        repair=repair,
    )


def _has_confirmed_inventory(pair: PairEntryQtyReconcile) -> bool:
    return (
        pair.yes.confirmed_qty > 0
        or pair.no.confirmed_qty > 0
        or pair.yes.effective_qty > 0
        or pair.no.effective_qty > 0
    )


async def _finalize_entry_from_reconcile(
    *,
    app,
    run_id,
    coord,
    sink,
    oms,
    strategy,
    cfg,
    state,
    pair: PairEntryQtyReconcile,
    apply_local_shadow_fill,
    live_clob_client,
    reconcile_reason: str,
) -> None:
    yes_tid = TokenId(cfg.yes_token_id)
    no_tid = TokenId(cfg.no_token_id)
    yes_book, no_book = _books(coord, cfg)
    yes_qty = pair.yes.effective_qty
    no_qty = pair.no.effective_qty
    min_eff = resolve_min_effective_pair_qty(
        cfg.min_effective_pair_qty,
        venue_min_size=app.risk.venue_min_size.default_min_size,
    )

    pb_facts.emit_entry_qty_reconciled(
        sink,
        run_id,
        state,
        yes_book,
        no_book,
        pair=pair,
        reason=reconcile_reason,
    )

    effective = min(yes_qty, no_qty)
    if effective < min_eff:
        if cfg.unwind_partial_entry:
            if yes_qty > 0:
                await _unwind_leg(
                    app=app,
                    run_id=run_id,
                    coord=coord,
                    sink=sink,
                    oms=oms,
                    strategy=strategy,
                    cfg=cfg,
                    state=state,
                    leg="yes",
                    token_id=yes_tid,
                    qty=yes_qty,
                    reason="below_min_effective_qty",
                    apply_local_shadow_fill=apply_local_shadow_fill,
                    live_clob_client=live_clob_client,
                )
            if no_qty > 0:
                await _unwind_leg(
                    app=app,
                    run_id=run_id,
                    coord=coord,
                    sink=sink,
                    oms=oms,
                    strategy=strategy,
                    cfg=cfg,
                    state=state,
                    leg="no",
                    token_id=no_tid,
                    qty=no_qty,
                    reason="below_min_effective_qty",
                    apply_local_shadow_fill=apply_local_shadow_fill,
                    live_clob_client=live_clob_client,
                )
        transition_phase(state, PairedBinaryPhase.FAILED, reason="below_min_effective_qty")
        return

    yes_entry = yes_book.ask or Decimal("0.5")
    no_entry = no_book.ask or Decimal("0.5")
    prices_ready = await _apply_entry_prices_from_fills(
        coord=coord,
        cfg=cfg,
        state=state,
        pair=pair,
        sink=sink,
        run_id=run_id,
        yes_book=yes_book,
        no_book=no_book,
        apply_local_shadow_fill=apply_local_shadow_fill,
    )
    if not prices_ready:
        state.yes.allocation_final_qty = yes_qty
        state.no.allocation_final_qty = no_qty
        return

    price_res = resolve_pair_entry_prices(
        coord,
        yes_token_id=cfg.yes_token_id,
        no_token_id=cfg.no_token_id,
        yes_client_order_id=state.yes.entry_client_order_id,
        no_client_order_id=state.no.entry_client_order_id,
        apply_shadow_fill=apply_local_shadow_fill,
        state=state,
    )
    from tyrex_pm.strategies.paired_binary.state import leg_entry_avg_price

    yes_entry = leg_entry_avg_price(state.yes) or price_res.yes.price
    no_entry = leg_entry_avg_price(state.no) or price_res.no.price
    assert yes_entry is not None and no_entry is not None
    yes_source = state.yes.entry_cash_source or price_res.yes.source
    no_source = state.no.entry_cash_source or price_res.no.source
    excess = mark_both_legs_filled(
        state,
        yes_qty=yes_qty,
        no_qty=no_qty,
        yes_entry=yes_entry,
        no_entry=no_entry,
        entry_price_source=yes_source,
        yes_entry_price_source=yes_source,
        no_entry_price_source=no_source,
    )
    old = PairedBinaryPhase.BOTH_ENTRY_PENDING
    pb_facts.emit_state_change(
        sink,
        run_id,
        state,
        yes_book,
        no_book,
        from_phase=old.value,
        to_phase=state.phase.value,
        reason="both_legs_final",
    )

    if excess and excess > 0 and cfg.unwind_partial_entry:
        heavy_leg = "yes" if yes_qty > no_qty else "no"
        heavy_tid = yes_tid if heavy_leg == "yes" else no_tid
        await _unwind_leg(
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            oms=oms,
            strategy=strategy,
            cfg=cfg,
            state=state,
            leg=heavy_leg,
            token_id=heavy_tid,
            qty=excess,
            reason="excess_qty",
            apply_local_shadow_fill=apply_local_shadow_fill,
            live_clob_client=live_clob_client,
        )


async def _unwind_unpaired_entry(
    *,
    app,
    run_id,
    coord,
    sink,
    oms,
    strategy,
    cfg,
    state,
    pair: PairEntryQtyReconcile,
    apply_local_shadow_fill,
    live_clob_client,
    reason: str,
    entry_timeout: bool,
) -> None:
    yes_tid = TokenId(cfg.yes_token_id)
    no_tid = TokenId(cfg.no_token_id)
    yes_book, no_book = _books(coord, cfg)
    yes_qty = pair.yes.effective_qty
    no_qty = pair.no.effective_qty

    if yes_qty > 0 and no_qty <= 0:
        if entry_timeout:
            pb_facts.emit_entry_timeout_unwind(
                sink, run_id, state, yes_book, no_book, leg="yes", qty=yes_qty, reason=reason
            )
        if cfg.abort_unpaired_entry and cfg.unwind_partial_entry:
            await _unwind_leg(
                app=app,
                run_id=run_id,
                coord=coord,
                sink=sink,
                oms=oms,
                strategy=strategy,
                cfg=cfg,
                state=state,
                leg="yes",
                token_id=yes_tid,
                qty=yes_qty,
                reason=reason,
                apply_local_shadow_fill=apply_local_shadow_fill,
                live_clob_client=live_clob_client,
            )
        transition_phase(state, PairedBinaryPhase.FAILED, reason=reason)
        return

    if no_qty > 0 and yes_qty <= 0:
        if entry_timeout:
            pb_facts.emit_entry_timeout_unwind(
                sink, run_id, state, yes_book, no_book, leg="no", qty=no_qty, reason=reason
            )
        if cfg.abort_unpaired_entry and cfg.unwind_partial_entry:
            await _unwind_leg(
                app=app,
                run_id=run_id,
                coord=coord,
                sink=sink,
                oms=oms,
                strategy=strategy,
                cfg=cfg,
                state=state,
                leg="no",
                token_id=no_tid,
                qty=no_qty,
                reason=reason,
                apply_local_shadow_fill=apply_local_shadow_fill,
                live_clob_client=live_clob_client,
            )
        transition_phase(state, PairedBinaryPhase.FAILED, reason=reason)


async def _unwind_leg(
    *,
    app,
    run_id,
    coord,
    sink,
    oms,
    strategy,
    cfg,
    state,
    leg: str,
    token_id: TokenId,
    qty: Decimal,
    reason: str,
    apply_local_shadow_fill: bool,
    live_clob_client,
    book: LegBook | None = None,
) -> UnwindLegResult:
    yes_book, no_book = _books(coord, cfg)
    if book is None:
        book = yes_book if leg == "yes" else no_book
    strategy.last_exit_blocked_leg = None
    strategy.last_exit_blocked_reason = None
    pb_facts.emit_unwind(sink, run_id, state, yes_book, no_book, leg=leg, reason=reason, qty=qty)
    bid = book.bid if book is not None and book.bid is not None else (yes_book.bid if leg == "yes" else no_book.bid)
    if bid is None:
        bid = Decimal("0.01")
    sizing = clamp_exit_size(
        coord,
        owner_id=cfg.owner_id,
        token_id=token_id,
        planned=qty,
        exit_order_style=cfg.exit_order_style,
    )
    leg_corr = f"{state.pair_correlation_id}:{leg}:unwind"
    w = build_exit_work_unit(
        token_id=token_id,
        size=sizing.final_size,
        limit_price=bid,
        order_style=cfg.exit_order_style,
        owner_id=cfg.owner_id,
        pair_correlation_id=state.pair_correlation_id or leg_corr,
        leg=leg,
        leg_correlation_id=leg_corr,
        reason=reason,
        sizing=sizing,
    )
    if w is None:
        return UnwindLegResult(
            leg=leg,
            submitted=False,
            blocked=True,
            risk_reason="SIZING_ZERO",
            allocation_qty=sizing.owner_allocation,
            venue_available_qty=sizing.venue_available,
            book_bid=bid,
            book_age_ms=book.book_age_ms if book else None,
            final_size=Decimal("0"),
        )
    await process_intent_work_unit(
        w,
        app=app,
        run_id=run_id,
        strategy=strategy,
        coord=coord,
        sink=sink,
        oms=oms,
        apply_local_shadow_fill=apply_local_shadow_fill,
        live_clob_client=live_clob_client,
    )
    blocked = strategy.last_exit_blocked_leg == leg
    risk_reason = strategy.last_exit_blocked_reason
    submitted = not blocked and strategy.last_exit_submitted_leg == leg
    return UnwindLegResult(
        leg=leg,
        submitted=submitted,
        blocked=blocked or not submitted,
        risk_reason=risk_reason,
        allocation_qty=sizing.owner_allocation,
        venue_available_qty=sizing.venue_available,
        book_bid=bid,
        book_age_ms=book.book_age_ms if book else None,
        final_size=sizing.final_size,
    )


async def _run_emergency_unwind(
    *,
    app,
    run_id,
    coord,
    sink,
    oms,
    strategy,
    cfg,
    state,
    qty: Decimal,
    reason: str,
    apply_local_shadow_fill,
    live_clob_client,
) -> EmergencyUnwindOutcome:
    yes_book, no_book = _books(coord, cfg)

    async def _unwind_fn(*, leg, token_id, qty, reason, book):
        return await _unwind_leg(
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
            reason=reason,
            book=book,
            apply_local_shadow_fill=apply_local_shadow_fill,
            live_clob_client=live_clob_client,
        )

    return await run_emergency_unwind_with_retry(
        cfg=cfg,
        state=state,
        yes_book=yes_book,
        no_book=no_book,
        qty=qty,
        reason=reason,
        unwind_leg_fn=_unwind_fn,
        emit_started=lambda **kw: pb_facts.emit_emergency_unwind_started(
            sink, run_id, state, yes_book, no_book, **kw
        ),
        emit_attempt=lambda **kw: pb_facts.emit_emergency_unwind_attempt(
            sink, run_id, state, yes_book, no_book, **kw
        ),
        emit_blocked=lambda **kw: pb_facts.emit_emergency_unwind_blocked(
            sink, run_id, state, yes_book, no_book, **kw
        ),
        emit_retry=lambda **kw: pb_facts.emit_emergency_unwind_retry(
            sink, run_id, state, yes_book, no_book, **kw
        ),
        emit_done=lambda **kw: pb_facts.emit_emergency_unwind_done(
            sink, run_id, state, yes_book, no_book, **kw
        ),
        emit_manual=lambda **kw: pb_facts.emit_manual_intervention_required(
            sink, run_id, state, yes_book, no_book, **kw
        ),
        get_leg_qty=lambda leg: leg_inventory_qty(coord, cfg, leg),
    )


async def _activate_monitoring_with_facts(
    *,
    sink,
    run_id,
    state,
    cfg,
    yes_book,
    no_book,
) -> None:
    activate_monitoring(state, yes_book=yes_book, no_book=no_book)
    yes_spread = (yes_book.ask - yes_book.bid) if yes_book.ask and yes_book.bid else None
    no_spread = (no_book.ask - no_book.bid) if no_book.ask and no_book.bid else None
    pb_facts.emit_book_capture_quality(
        sink, run_id, state, yes_book, no_book, event="activation"
    )
    pb_facts.emit_activation_reference(sink, run_id, state, yes_book, no_book)
    pb_facts.emit_pnl_plan(
        sink,
        run_id,
        state,
        yes_book,
        no_book,
        cfg=cfg,
        yes_spread=yes_spread,
        no_spread=no_spread,
    )
    pb_facts.emit_monitor_started(sink, run_id, state, yes_book, no_book)
    pb_facts.emit_state_change(
        sink,
        run_id,
        state,
        yes_book,
        no_book,
        from_phase=PairedBinaryPhase.BOTH_LEGS_FILLED.value,
        to_phase=state.phase.value,
        reason="both_legs_sellable",
    )
    tracker = LatencyTracker()
    tracker.set_book_capture(yes_book, no_book)
    tracker.mark_sellable_seen()
    pb_facts.emit_latency_sample(
        sink, run_id, state, payload=tracker.payload(event="activation")
    )


async def _handle_activation_failure(
    *,
    app,
    run_id,
    coord,
    sink,
    oms,
    strategy,
    cfg,
    state,
    yes_book,
    no_book,
    safety,
    apply_local_shadow_fill,
    live_clob_client,
) -> None:
    pb_facts.emit_activation_rejected_loss_budget(
        sink,
        run_id,
        state,
        yes_book,
        no_book,
        reason=safety.reason or "activation_gap_exceeds_loss_budget",
    )
    if state.effective_qty > 0:
        outcome = await _run_emergency_unwind(
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            oms=oms,
            strategy=strategy,
            cfg=cfg,
            state=state,
            qty=state.effective_qty,
            reason=safety.reason or "activation_gap_exceeds_loss_budget",
            apply_local_shadow_fill=apply_local_shadow_fill,
            live_clob_client=live_clob_client,
        )
        state.unwind_attempt_count = outcome.attempt_count
        if outcome.manual_intervention:
            transition_phase(
                state,
                PairedBinaryPhase.FAILED,
                reason="manual_intervention_required",
            )
            return
    transition_phase(state, PairedBinaryPhase.FAILED, reason=safety.reason)


async def _complete_entry_phase(
    *,
    app,
    run_id,
    coord,
    sink,
    oms,
    strategy,
    cfg,
    state,
    apply_local_shadow_fill,
    live_clob_client,
    entry_timeout: bool = False,
) -> None:
    pair = await _reconcile_pair_entry(
        app=app,
        run_id=run_id,
        coord=coord,
        cfg=cfg,
        state=state,
        repair=True,
    )
    yes_qty = pair.yes.effective_qty
    no_qty = pair.no.effective_qty
    min_eff = resolve_min_effective_pair_qty(
        cfg.min_effective_pair_qty,
        venue_min_size=app.risk.venue_min_size.default_min_size,
    )

    if yes_qty <= 0 and no_qty <= 0:
        if entry_timeout and _has_confirmed_inventory(pair):
            await _unwind_unpaired_entry(
                app=app,
                run_id=run_id,
                coord=coord,
                sink=sink,
                oms=oms,
                strategy=strategy,
                cfg=cfg,
                state=state,
                pair=pair,
                apply_local_shadow_fill=apply_local_shadow_fill,
                live_clob_client=live_clob_client,
                reason="entry_timeout_unwind",
                entry_timeout=True,
            )
            return
        transition_phase(
            state,
            PairedBinaryPhase.FAILED,
            reason="entry_timeout" if entry_timeout else "entry_incomplete",
        )
        return

    if (yes_qty > 0) != (no_qty > 0):
        yes_book, no_book = _books(coord, cfg)
        blocked = "no" if yes_qty > no_qty else "yes"
        reason = "entry_timeout_unwind" if entry_timeout else "unpaired_entry"
        if entry_timeout:
            pb_facts.emit_entry_timeout_unwind_retry(
                sink,
                run_id,
                state,
                yes_book,
                no_book,
                reason=reason,
                blocked_leg=blocked,
            )
        await abort_pair_entry(
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
            reason=reason,
            blocked_leg=blocked,
            blocking_phase="entry_timeout" if entry_timeout else "reconcile",
            reason_codes=(reason,),
            apply_local_shadow_fill=apply_local_shadow_fill,
            live_clob_client=live_clob_client,
            unwind_fn=_run_emergency_unwind,
        )
        return

    if min(yes_qty, no_qty) >= min_eff:
        await _finalize_entry_from_reconcile(
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            oms=oms,
            strategy=strategy,
            cfg=cfg,
            state=state,
            pair=pair,
            apply_local_shadow_fill=apply_local_shadow_fill,
            live_clob_client=live_clob_client,
            reconcile_reason="entry_timeout_repair" if entry_timeout else "entry_ready",
        )
        return

    if cfg.unwind_partial_entry:
        yes_tid = TokenId(cfg.yes_token_id)
        no_tid = TokenId(cfg.no_token_id)
        if yes_qty > 0:
            await _unwind_leg(
                app=app,
                run_id=run_id,
                coord=coord,
                sink=sink,
                oms=oms,
                strategy=strategy,
                cfg=cfg,
                state=state,
                leg="yes",
                token_id=yes_tid,
                qty=yes_qty,
                reason="below_min_effective_qty",
                apply_local_shadow_fill=apply_local_shadow_fill,
                live_clob_client=live_clob_client,
            )
        if no_qty > 0:
            await _unwind_leg(
                app=app,
                run_id=run_id,
                coord=coord,
                sink=sink,
                oms=oms,
                strategy=strategy,
                cfg=cfg,
                state=state,
                leg="no",
                token_id=no_tid,
                qty=no_qty,
                reason="below_min_effective_qty",
                apply_local_shadow_fill=apply_local_shadow_fill,
                live_clob_client=live_clob_client,
            )
    transition_phase(state, PairedBinaryPhase.FAILED, reason="below_min_effective_qty")


async def _try_early_entry_completion(
    *,
    app,
    run_id,
    coord,
    sink,
    oms,
    strategy,
    cfg,
    state,
    apply_local_shadow_fill,
    live_clob_client,
) -> bool:
    pair = await _reconcile_pair_entry(
        app=app,
        run_id=run_id,
        coord=coord,
        cfg=cfg,
        state=state,
        repair=True,
    )
    min_eff = resolve_min_effective_pair_qty(
        cfg.min_effective_pair_qty,
        venue_min_size=app.risk.venue_min_size.default_min_size,
    )
    if pair.yes.effective_qty < min_eff or pair.no.effective_qty < min_eff:
        return False
    if pair.yes.effective_qty <= 0 or pair.no.effective_qty <= 0:
        return False
    await _finalize_entry_from_reconcile(
        app=app,
        run_id=run_id,
        coord=coord,
        sink=sink,
        oms=oms,
        strategy=strategy,
        cfg=cfg,
        state=state,
        pair=pair,
        apply_local_shadow_fill=apply_local_shadow_fill,
        live_clob_client=live_clob_client,
        reconcile_reason="early_entry_ready",
    )
    return True


def _update_post_exit_state(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
    coord,
    *,
    sink=None,
    run_id: RunId | None = None,
    yes_book: LegBook | None = None,
    no_book: LegBook | None = None,
) -> None:
    ledger = coord.allocation_ledger
    if ledger is None:
        return
    yes_qty = ledger.get_available_allocated(cfg.owner_id, TokenId(cfg.yes_token_id))
    no_qty = ledger.get_available_allocated(cfg.owner_id, TokenId(cfg.no_token_id))
    if yes_qty <= 0 and no_qty <= 0:
        state.phase = PairedBinaryPhase.DONE
        return

    from tyrex_pm.strategies.paired_binary.state import leg_exit_avg_price

    phase = state.phase

    if phase in {PairedBinaryPhase.EXITING_YES, PairedBinaryPhase.STOP_PENDING_YES, PairedBinaryPhase.TP_PENDING_YES}:
        if yes_qty <= 0:
            fill = leg_exit_avg_price(state.yes) or state.yes.last_exit_bid
            if fill is not None:
                state.yes_exit = fill
                if leg_exit_avg_price(state.yes) is not None:
                    state.yes.exit_fill_price = leg_exit_avg_price(state.yes)
            state.yes.exit_submitted = True
            state.yes.pending_trigger_type = None
            if no_qty > 0:
                state.phase = PairedBinaryPhase.ONLY_NO_ACTIVE
                state.effective_qty = no_qty
                if fill is not None and state.yes.triggered and yes_book and no_book and sink and run_id:
                    old, new = reprice_survivor_after_loser_exit(
                        state, cfg, loser_leg="yes", loser_exit_fill=fill
                    )
                    if old != new and new is not None:
                        from tyrex_pm.strategies.paired_binary.pnl import reprice_survivor_target_after_loser_exit

                        plan = reprice_survivor_target_after_loser_exit(
                            survivor_entry=state.no_entry or Decimal("0"),
                            loser_entry=state.yes_entry or Decimal("0"),
                            loser_exit_fill=fill,
                            pair_cost=state.pair_cost or Decimal("0"),
                            pair_stop_loss_pct=cfg.pair_stop_loss_pct,
                            pair_take_profit_pct=cfg.pair_take_profit_pct,
                            slippage_buffer=cfg.slippage_buffer,
                        )
                        pb_facts.emit_winner_target_repriced(
                            sink,
                            run_id,
                            state,
                            yes_book,
                            no_book,
                            survivor_leg="no",
                            realized_loser_loss=plan.realized_loser_loss or Decimal("0"),
                            required_winner_gain=plan.required_winner_gain or Decimal("0"),
                            old_target=old,
                            new_target=new,
                        )
            else:
                state.phase = PairedBinaryPhase.DONE
            return

    if phase in {PairedBinaryPhase.EXITING_NO, PairedBinaryPhase.STOP_PENDING_NO, PairedBinaryPhase.TP_PENDING_NO}:
        if no_qty <= 0:
            fill = leg_exit_avg_price(state.no) or state.no.last_exit_bid
            if fill is not None:
                state.no_exit = fill
                if leg_exit_avg_price(state.no) is not None:
                    state.no.exit_fill_price = leg_exit_avg_price(state.no)
            state.no.exit_submitted = True
            state.no.pending_trigger_type = None
            if yes_qty > 0:
                state.phase = PairedBinaryPhase.ONLY_YES_ACTIVE
                state.effective_qty = yes_qty
                if fill is not None and state.no.triggered and yes_book and no_book and sink and run_id:
                    old, new = reprice_survivor_after_loser_exit(
                        state, cfg, loser_leg="no", loser_exit_fill=fill
                    )
                    if old != new and new is not None:
                        from tyrex_pm.strategies.paired_binary.pnl import reprice_survivor_target_after_loser_exit

                        plan = reprice_survivor_target_after_loser_exit(
                            survivor_entry=state.yes_entry or Decimal("0"),
                            loser_entry=state.no_entry or Decimal("0"),
                            loser_exit_fill=fill,
                            pair_cost=state.pair_cost or Decimal("0"),
                            pair_stop_loss_pct=cfg.pair_stop_loss_pct,
                            pair_take_profit_pct=cfg.pair_take_profit_pct,
                            slippage_buffer=cfg.slippage_buffer,
                        )
                        pb_facts.emit_winner_target_repriced(
                            sink,
                            run_id,
                            state,
                            yes_book,
                            no_book,
                            survivor_leg="yes",
                            realized_loser_loss=plan.realized_loser_loss or Decimal("0"),
                            required_winner_gain=plan.required_winner_gain or Decimal("0"),
                            old_target=old,
                            new_target=new,
                        )
            else:
                state.phase = PairedBinaryPhase.DONE
            return

    if phase == PairedBinaryPhase.EXITING_BOTH:
        if yes_qty <= 0 and no_qty <= 0:
            state.phase = PairedBinaryPhase.DONE
        elif yes_qty <= 0:
            state.phase = PairedBinaryPhase.ONLY_NO_ACTIVE
            state.effective_qty = no_qty
        elif no_qty <= 0:
            state.phase = PairedBinaryPhase.ONLY_YES_ACTIVE
            state.effective_qty = yes_qty

    if phase == PairedBinaryPhase.TIMEOUT_PENDING:
        if yes_qty <= 0 and no_qty <= 0:
            state.phase = PairedBinaryPhase.DONE
        elif yes_qty <= 0 and no_qty > 0:
            state.phase = PairedBinaryPhase.ONLY_NO_ACTIVE
            state.effective_qty = no_qty
        elif no_qty <= 0 and yes_qty > 0:
            state.phase = PairedBinaryPhase.ONLY_YES_ACTIVE
            state.effective_qty = yes_qty


async def run_paired_binary_loop(
    *,
    app: AppConfig,
    run_id: RunId,
    coord,
    sink,
    oms,
    cfg: PairedBinaryStrategyConfig,
    state: PairedBinaryRuntimeState,
    state_dir: Path,
    apply_local_shadow_fill: bool = True,
    live_clob_client: object | None = None,
    stop: asyncio.Event | None = None,
) -> int:
    """Run paired binary until terminal or stop. Returns tick count."""
    strategy = PairedBinaryStrategy(cfg)
    strategy.bind_state(state)
    monitor = PairedBinaryMonitor(cfg)
    persist_path = persistence_path(state_dir, cfg.owner_id, cfg.market_id)

    _inject_fixture_books(coord, cfg)
    if live_clob_client is not None:
        await bootstrap_market_state(coord, app, live_clob_client=live_clob_client)

    ticks = 0
    max_ticks = max(1, int(cfg.max_runtime_s / cfg.tick_interval_s)) if cfg.max_runtime_s else 10_000

    while ticks < max_ticks:
        if stop is not None and stop.is_set():
            break
        if state.is_terminal():
            break

        yes_book, no_book = _books(coord, cfg)

        if state.phase == PairedBinaryPhase.IDLE:
            if cfg.entry_dry_run:
                eval_inp = _entry_eval_input(cfg, yes_book, no_book)
                ev = evaluate_entry(eval_inp)
                if ev.allowed and ev.pair_cost and ev.yes_spread and ev.no_spread:
                    pb_facts.emit_entry_eval(
                        sink,
                        run_id,
                        state,
                        yes_book,
                        no_book,
                        pair_cost=ev.pair_cost,
                        yes_spread=ev.yes_spread,
                        no_spread=ev.no_spread,
                        estimated_loss_budget=ev.estimated_loss_budget,
                        slippage_buffer=ev.slippage_buffer,
                    )
                elif ev.reason:
                    pb_facts.emit_entry_skip(
                        sink,
                        run_id,
                        state,
                        yes_book,
                        no_book,
                        reason=ev.reason,
                        pair_cost=ev.pair_cost,
                        yes_spread=ev.yes_spread,
                        no_spread=ev.no_spread,
                        estimated_loss_budget=ev.estimated_loss_budget,
                        slippage_buffer=ev.slippage_buffer,
                    )
                break
            state.pair_correlation_id = f"paired_binary_{uuid.uuid4().hex[:12]}"
            await _submit_entry_intents(
                app=app,
                run_id=run_id,
                coord=coord,
                sink=sink,
                oms=oms,
                strategy=strategy,
                cfg=cfg,
                state=state,
                pair_correlation_id=state.pair_correlation_id,
                apply_local_shadow_fill=apply_local_shadow_fill,
                live_clob_client=live_clob_client,
            )
            save_persisted_state(persist_path, state)

        elif state.phase == PairedBinaryPhase.BOTH_ENTRY_PENDING:
            action = await tick_pair_entry_pending(
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
                unwind_fn=_run_emergency_unwind,
            )
            if action == "fill_timeout":
                yes_q = _leg_filled_qty(coord, cfg, "yes")
                no_q = _leg_filled_qty(coord, cfg, "no")
                blocked = "no" if yes_q > no_q else ("yes" if no_q > yes_q else "unknown")
                pb_facts.emit_entry_timeout_unwind_retry(
                    sink,
                    run_id,
                    state,
                    yes_book,
                    no_book,
                    reason="entry_fill_timeout",
                    blocked_leg=blocked,
                )
                await abort_pair_entry(
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
                    reason="entry_fill_timeout",
                    blocked_leg=None,
                    blocking_phase="fill_timeout",
                    reason_codes=("entry_fill_timeout",),
                    apply_local_shadow_fill=apply_local_shadow_fill,
                    live_clob_client=live_clob_client,
                    unwind_fn=_run_emergency_unwind,
                )
            elif action in {"committed", "pending"}:
                await _try_early_entry_completion(
                    app=app,
                    run_id=run_id,
                    coord=coord,
                    sink=sink,
                    oms=oms,
                    strategy=strategy,
                    cfg=cfg,
                    state=state,
                    apply_local_shadow_fill=apply_local_shadow_fill,
                    live_clob_client=live_clob_client,
                )

        elif state.phase == PairedBinaryPhase.BOTH_LEGS_FILLED:
            sellable, yes_sell, no_sell = both_legs_sellable(coord, cfg, state)
            if sellable:
                safety = evaluate_activation_safety(state, cfg, yes_book, no_book)
                if not safety.ok:
                    if should_use_activation_recheck(cfg):
                        begin_activation_recheck(state)
                        state.activation_recheck_attempts = 1
                        pb_facts.emit_activation_gap_recheck(
                            sink,
                            run_id,
                            state,
                            yes_book,
                            no_book,
                            cfg,
                            attempt_count=1,
                            elapsed_s=0.0,
                            reason=safety.reason or "activation_gap_exceeds_loss_budget",
                        )
                    else:
                        await _handle_activation_failure(
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
                            safety=safety,
                            apply_local_shadow_fill=apply_local_shadow_fill,
                            live_clob_client=live_clob_client,
                        )
                else:
                    await _activate_monitoring_with_facts(
                        sink=sink,
                        run_id=run_id,
                        state=state,
                        cfg=cfg,
                        yes_book=yes_book,
                        no_book=no_book,
                    )
            else:
                pb_facts.emit_waiting_for_sellable_inventory(
                    sink,
                    run_id,
                    state,
                    yes_book,
                    no_book,
                    yes_sell=yes_sell,
                    no_sell=no_sell,
                )

        elif state.phase == PairedBinaryPhase.ACTIVATION_PENDING_RECHECK:
            sellable, yes_sell, no_sell = both_legs_sellable(coord, cfg, state)
            if not sellable:
                pb_facts.emit_waiting_for_sellable_inventory(
                    sink,
                    run_id,
                    state,
                    yes_book,
                    no_book,
                    yes_sell=yes_sell,
                    no_sell=no_sell,
                )
            else:
                elapsed = activation_recheck_elapsed_s(state)
                state.activation_recheck_attempts += 1
                safety = evaluate_activation_safety(state, cfg, yes_book, no_book)
                if safety.ok:
                    pb_facts.emit_activation_recovered(
                        sink,
                        run_id,
                        state,
                        yes_book,
                        no_book,
                        cfg,
                        attempt_count=state.activation_recheck_attempts,
                        elapsed_s=elapsed,
                    )
                    await _activate_monitoring_with_facts(
                        sink=sink,
                        run_id=run_id,
                        state=state,
                        cfg=cfg,
                        yes_book=yes_book,
                        no_book=no_book,
                    )
                elif activation_recheck_expired(state, cfg):
                    await _handle_activation_failure(
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
                        safety=safety,
                        apply_local_shadow_fill=apply_local_shadow_fill,
                        live_clob_client=live_clob_client,
                    )
                else:
                    pb_facts.emit_activation_gap_recheck(
                        sink,
                        run_id,
                        state,
                        yes_book,
                        no_book,
                        cfg,
                        attempt_count=state.activation_recheck_attempts,
                        elapsed_s=elapsed,
                        reason=safety.reason or "activation_gap_exceeds_loss_budget",
                    )
                    await asyncio.sleep(cfg.activation_gap_retry_interval_s)

        elif state.phase == PairedBinaryPhase.UNWIND_PENDING:
            yes_qty = leg_inventory_qty(coord, cfg, "yes")
            no_qty = leg_inventory_qty(coord, cfg, "no")
            if yes_qty <= 0 and no_qty <= 0:
                transition_phase(state, PairedBinaryPhase.DONE, reason="emergency_unwind_complete")
            elif state.unwind_block_reason:
                outcome = await _run_emergency_unwind(
                    app=app,
                    run_id=run_id,
                    coord=coord,
                    sink=sink,
                    oms=oms,
                    strategy=strategy,
                    cfg=cfg,
                    state=state,
                    qty=state.effective_qty,
                    reason=state.unwind_block_reason,
                    apply_local_shadow_fill=apply_local_shadow_fill,
                    live_clob_client=live_clob_client,
                )
                state.unwind_attempt_count = outcome.attempt_count
                if outcome.flat:
                    transition_phase(state, PairedBinaryPhase.DONE, reason="emergency_unwind_complete")
                elif outcome.manual_intervention:
                    transition_phase(
                        state, PairedBinaryPhase.FAILED, reason="manual_intervention_required"
                    )

        elif state.phase in {
            PairedBinaryPhase.BOTH_LEGS_ACTIVE,
            PairedBinaryPhase.ONLY_YES_ACTIVE,
            PairedBinaryPhase.ONLY_NO_ACTIVE,
            PairedBinaryPhase.STOP_PENDING_YES,
            PairedBinaryPhase.STOP_PENDING_NO,
            PairedBinaryPhase.TP_PENDING_YES,
            PairedBinaryPhase.TP_PENDING_NO,
            PairedBinaryPhase.TIMEOUT_PENDING,
            PairedBinaryPhase.EXITING_YES,
            PairedBinaryPhase.EXITING_NO,
            PairedBinaryPhase.EXITING_BOTH,
        }:
            work = monitor.tick(coord, state, sink=sink, run_id=run_id)
            for w in work:
                leg = (w.intent_fact_extensions or {}).get("leg", "yes")
                await process_intent_work_unit(
                    w,
                    app=app,
                    run_id=run_id,
                    strategy=strategy,
                    coord=coord,
                    sink=sink,
                    oms=oms,
                    apply_local_shadow_fill=apply_local_shadow_fill,
                    live_clob_client=live_clob_client,
                )
                if getattr(strategy, "last_exit_submitted_leg", None) == leg:
                    confirm_exit_submitted(state, leg)  # type: ignore[arg-type]
            _update_post_exit_state(
                state,
                cfg,
                coord,
                sink=sink,
                run_id=run_id,
                yes_book=yes_book,
                no_book=no_book,
            )
            if state.phase == PairedBinaryPhase.DONE:
                pb_facts.emit_realized_pnl(sink, run_id, state, yes_book, no_book)
                pb_facts.emit_done(sink, run_id, state, yes_book, no_book)

        save_persisted_state(persist_path, state)
        ticks += 1

        if state.phase == PairedBinaryPhase.BOTH_LEGS_ACTIVE and cfg.stop_after_entry:
            break
        if state.is_terminal():
            break
        await asyncio.sleep(cfg.tick_interval_s)

    sink.write(
        make_fact(
            FACT_TYPE_HEALTH,
            str(run_id),
            {
                "event": "paired_binary_loop_stopped",
                "ticks": ticks,
                "final_state": state.phase.value,
            },
        )
    )
    return ticks
