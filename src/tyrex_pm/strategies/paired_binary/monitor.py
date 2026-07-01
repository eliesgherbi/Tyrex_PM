"""Paired binary monitor — emits ExitIntent work units only (Phase 4.6)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import AppConfig, PairedBinaryStrategyConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.intent_work import IntentWorkUnit
from tyrex_pm.strategies.paired_binary import facts as pb_facts
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook, read_leg_book
from tyrex_pm.strategies.paired_binary.exit_engine import (
    MONITOR_TICK_PHASES,
    evaluate_dual_stop,
    prepare_no_stop_trigger,
    prepare_timeout_trigger,
    prepare_tp_trigger,
    prepare_yes_stop_trigger,
    record_exit_blocked,
    try_build_exit,
)
from tyrex_pm.strategies.paired_binary.observability import (
    emit_material_decision,
    new_decision_id,
    trigger_to_context,
)
from tyrex_pm.market_data.decision_gate import should_block_paired_binary_decision
from tyrex_pm.market_data.quality import DecisionContext
from tyrex_pm.market_data.readiness_runtime import emit_market_data_health_block
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState


class PairedBinaryMonitor:
    """Strategy-specific monitor; never submits to OMS."""

    def __init__(self, cfg: PairedBinaryStrategyConfig) -> None:
        self._cfg = cfg

    def _emit_trigger_snapshot(
        self,
        *,
        app: AppConfig | None,
        coord: RuntimeCoordinator,
        state: PairedBinaryRuntimeState,
        sink: JsonlSink | None,
        run_id: RunId | None,
        decision_type: str,
        trigger_type: str,
        is_retry: bool = False,
    ) -> str | None:
        if app is None or sink is None or run_id is None:
            return None
        dtype = "fak_retry" if is_retry else decision_type
        return emit_material_decision(
            app=app,
            coord=coord,
            sink=sink,
            run_id=run_id,
            cfg=self._cfg,
            state=state,
            decision_type=dtype,
            context=trigger_to_context(trigger_type),
            size=state.effective_qty,
            emit_latency=not is_retry,
        )

    def _books(self, coord: RuntimeCoordinator) -> tuple[LegBook, LegBook]:
        ms = coord.market_state
        max_age = float(self._cfg.max_book_age_s)
        yes = read_leg_book(ms, TokenId(self._cfg.yes_token_id), max_book_age_s=max_age)
        no = read_leg_book(ms, TokenId(self._cfg.no_token_id), max_book_age_s=max_age)
        return yes, no

    def tick(
        self,
        coord: RuntimeCoordinator,
        state: PairedBinaryRuntimeState,
        *,
        sink: JsonlSink | None = None,
        run_id: RunId | None = None,
        app: AppConfig | None = None,
    ) -> list[IntentWorkUnit]:
        if state.phase not in MONITOR_TICK_PHASES:
            return []

        yes_book, no_book = self._books(coord)
        book_stale = yes_book.stale or no_book.stale
        if book_stale or yes_book.bid is None or no_book.bid is None:
            return []

        cfg = self._cfg
        pair_id = state.pair_correlation_id or "paired_binary_unknown"
        work: list[IntentWorkUnit] = []

        timeout_work = self._maybe_timeout(coord, state, yes_book, no_book, sink, run_id, app=app)
        if timeout_work is not None:
            return timeout_work

        pending_work = self._retry_pending_exits(coord, state, yes_book, no_book, sink, run_id, app=app)
        if pending_work:
            return pending_work

        if state.phase == PairedBinaryPhase.BOTH_LEGS_ACTIVE:
            stop_leg = evaluate_dual_stop(state, cfg, yes_book, no_book)
            if stop_leg is None:
                return []
            if stop_leg == "yes":
                prepare_yes_stop_trigger(state, cfg)
                if sink and run_id:
                    pb_facts.emit_stop_plan(
                        sink,
                        run_id,
                        state,
                        yes_book,
                        no_book,
                        cfg=cfg,
                        leg="yes",
                    )
                    pb_facts.emit_leg_stop(
                        sink, run_id, state, yes_book, no_book, leg="yes", target_price=state.no_target
                    )
                decision_id = self._emit_trigger_snapshot(
                    app=app,
                    coord=coord,
                    state=state,
                    sink=sink,
                    run_id=run_id,
                    decision_type="stop_trigger",
                    trigger_type="stop_loss",
                )
                return self._dispatch_exit(
                    coord,
                    state,
                    leg="yes",
                    trigger_type="stop_loss",
                    bid=yes_book.bid,
                    qty=state.effective_qty,
                    pair_id=pair_id,
                    target_price=state.no_target,
                    yes_book=yes_book,
                    no_book=no_book,
                    sink=sink,
                    run_id=run_id,
                    app=app,
                    decision_id=decision_id,
                )
            prepare_no_stop_trigger(state, cfg)
            if sink and run_id:
                pb_facts.emit_stop_plan(
                    sink,
                    run_id,
                    state,
                    yes_book,
                    no_book,
                    cfg=cfg,
                    leg="no",
                )
                pb_facts.emit_leg_stop(
                    sink, run_id, state, yes_book, no_book, leg="no", target_price=state.yes_target
                )
            decision_id = self._emit_trigger_snapshot(
                app=app,
                coord=coord,
                state=state,
                sink=sink,
                run_id=run_id,
                decision_type="stop_trigger",
                trigger_type="stop_loss",
            )
            return self._dispatch_exit(
                coord,
                state,
                leg="no",
                trigger_type="stop_loss",
                bid=no_book.bid,
                qty=state.effective_qty,
                pair_id=pair_id,
                target_price=state.yes_target,
                yes_book=yes_book,
                no_book=no_book,
                sink=sink,
                run_id=run_id,
                app=app,
                decision_id=decision_id,
            )

        if state.phase == PairedBinaryPhase.ONLY_YES_ACTIVE:
            if (
                state.yes_target is not None
                and yes_book.bid >= state.yes_target
                and not state.yes.exit_submitted
            ):
                prepare_tp_trigger(state, "yes")
                if sink and run_id:
                    pb_facts.emit_winner_target_plan(
                        sink,
                        run_id,
                        state,
                        yes_book,
                        no_book,
                        cfg=cfg,
                        leg="yes",
                    )
                    pb_facts.emit_winner_target(
                        sink, run_id, state, yes_book, no_book, leg="yes", target_price=state.yes_target
                    )
                decision_id = self._emit_trigger_snapshot(
                    app=app,
                    coord=coord,
                    state=state,
                    sink=sink,
                    run_id=run_id,
                    decision_type="take_profit_trigger",
                    trigger_type="take_profit",
                )
                return self._dispatch_exit(
                    coord,
                    state,
                    leg="yes",
                    trigger_type="take_profit",
                    bid=yes_book.bid,
                    qty=state.effective_qty,
                    pair_id=pair_id,
                    target_price=state.yes_target,
                    yes_book=yes_book,
                    no_book=no_book,
                    sink=sink,
                    run_id=run_id,
                    app=app,
                    decision_id=decision_id,
                )

        if state.phase == PairedBinaryPhase.ONLY_NO_ACTIVE:
            if (
                state.no_target is not None
                and no_book.bid >= state.no_target
                and not state.no.exit_submitted
            ):
                prepare_tp_trigger(state, "no")
                if sink and run_id:
                    pb_facts.emit_winner_target_plan(
                        sink,
                        run_id,
                        state,
                        yes_book,
                        no_book,
                        cfg=cfg,
                        leg="no",
                    )
                    pb_facts.emit_winner_target(
                        sink, run_id, state, yes_book, no_book, leg="no", target_price=state.no_target
                    )
                decision_id = self._emit_trigger_snapshot(
                    app=app,
                    coord=coord,
                    state=state,
                    sink=sink,
                    run_id=run_id,
                    decision_type="take_profit_trigger",
                    trigger_type="take_profit",
                )
                return self._dispatch_exit(
                    coord,
                    state,
                    leg="no",
                    trigger_type="take_profit",
                    bid=no_book.bid,
                    qty=state.effective_qty,
                    pair_id=pair_id,
                    target_price=state.no_target,
                    yes_book=yes_book,
                    no_book=no_book,
                    sink=sink,
                    run_id=run_id,
                    app=app,
                    decision_id=decision_id,
                )

        return work

    def _maybe_timeout(
        self,
        coord: RuntimeCoordinator,
        state: PairedBinaryRuntimeState,
        yes_book: LegBook,
        no_book: LegBook,
        sink: JsonlSink | None,
        run_id: RunId | None,
        *,
        app: AppConfig | None = None,
    ) -> list[IntentWorkUnit] | None:
        cfg = self._cfg
        if state.pair_opened_ts is None:
            return None
        if (monotonic_s() - state.pair_opened_ts) < cfg.max_holding_time_s:
            return None

        pair_id = state.pair_correlation_id or "paired_binary_unknown"
        phase = state.phase

        if phase == PairedBinaryPhase.TIMEOUT_PENDING:
            return self._retry_pending_exits(coord, state, yes_book, no_book, sink, run_id, app=app)

        if sink and run_id:
            pb_facts.emit_timeout_exit(sink, run_id, state, yes_book, no_book)

        if phase == PairedBinaryPhase.BOTH_LEGS_ACTIVE:
            prepare_timeout_trigger(state, legs=("yes", "no"))
            work: list[IntentWorkUnit] = []
            for leg, bid in (("yes", yes_book.bid), ("no", no_book.bid)):
                w = self._dispatch_exit(
                    coord,
                    state,
                    leg=leg,  # type: ignore[arg-type]
                    trigger_type="timeout",
                    bid=bid,
                    qty=state.effective_qty,
                    pair_id=pair_id,
                    target_price=None,
                    yes_book=yes_book,
                    no_book=no_book,
                    sink=sink,
                    run_id=run_id,
                    app=app,
                    append=True,
                )
                work.extend(w)
            return work

        if phase in {
            PairedBinaryPhase.STOP_PENDING_YES,
            PairedBinaryPhase.STOP_PENDING_NO,
            PairedBinaryPhase.TP_PENDING_YES,
            PairedBinaryPhase.TP_PENDING_NO,
        }:
            leg = self._pending_leg_from_phase(phase)
            rt = state.yes if leg == "yes" else state.no
            trigger_type = rt.pending_trigger_type or "stop_loss"
            bid = yes_book.bid if leg == "yes" else no_book.bid
            return self._dispatch_exit(
                coord,
                state,
                leg=leg,
                trigger_type=trigger_type,  # type: ignore[arg-type]
                bid=bid,
                qty=state.effective_qty,
                pair_id=pair_id,
                target_price=state.yes_target if leg == "yes" else state.no_target,
                yes_book=yes_book,
                no_book=no_book,
                sink=sink,
                run_id=run_id,
                app=app,
            )

        if phase == PairedBinaryPhase.ONLY_YES_ACTIVE and not state.yes.exit_submitted:
            prepare_timeout_trigger(state, legs=("yes",))
            return self._dispatch_exit(
                coord,
                state,
                leg="yes",
                trigger_type="timeout",
                bid=yes_book.bid,
                qty=state.effective_qty,
                pair_id=pair_id,
                target_price=state.yes_target,
                yes_book=yes_book,
                no_book=no_book,
                sink=sink,
                run_id=run_id,
                app=app,
            )

        if phase == PairedBinaryPhase.ONLY_NO_ACTIVE and not state.no.exit_submitted:
            prepare_timeout_trigger(state, legs=("no",))
            return self._dispatch_exit(
                coord,
                state,
                leg="no",
                trigger_type="timeout",
                bid=no_book.bid,
                qty=state.effective_qty,
                pair_id=pair_id,
                target_price=state.no_target,
                yes_book=yes_book,
                no_book=no_book,
                sink=sink,
                run_id=run_id,
                app=app,
            )

        if phase in {
            PairedBinaryPhase.EXITING_YES,
            PairedBinaryPhase.EXITING_NO,
            PairedBinaryPhase.EXITING_BOTH,
        }:
            return self._retry_stuck_exiting(coord, state, yes_book, no_book, sink, run_id, app=app)

        return None

    def _retry_stuck_exiting(
        self,
        coord: RuntimeCoordinator,
        state: PairedBinaryRuntimeState,
        yes_book: LegBook,
        no_book: LegBook,
        sink: JsonlSink | None,
        run_id: RunId | None,
        *,
        app: AppConfig | None = None,
    ) -> list[IntentWorkUnit]:
        pair_id = state.pair_correlation_id or "paired_binary_unknown"
        work: list[IntentWorkUnit] = []
        if state.phase in {PairedBinaryPhase.EXITING_YES, PairedBinaryPhase.EXITING_BOTH}:
            if not state.yes.exit_submitted:
                state.phase = PairedBinaryPhase.STOP_PENDING_YES
                work.extend(
                    self._dispatch_exit(
                        coord,
                        state,
                        leg="yes",
                        trigger_type="timeout",
                        bid=yes_book.bid,
                        qty=state.effective_qty,
                        pair_id=pair_id,
                        target_price=state.yes_target,
                        yes_book=yes_book,
                        no_book=no_book,
                        sink=sink,
                        run_id=run_id,
                    )
                )
        if state.phase in {PairedBinaryPhase.EXITING_NO, PairedBinaryPhase.EXITING_BOTH}:
            if not state.no.exit_submitted:
                state.phase = PairedBinaryPhase.STOP_PENDING_NO
                work.extend(
                    self._dispatch_exit(
                        coord,
                        state,
                        leg="no",
                        trigger_type="timeout",
                        bid=no_book.bid,
                        qty=state.effective_qty,
                        pair_id=pair_id,
                        target_price=state.no_target,
                        yes_book=yes_book,
                        no_book=no_book,
                        sink=sink,
                        run_id=run_id,
                    )
                )
        return work

    def _retry_pending_exits(
        self,
        coord: RuntimeCoordinator,
        state: PairedBinaryRuntimeState,
        yes_book: LegBook,
        no_book: LegBook,
        sink: JsonlSink | None,
        run_id: RunId | None,
        *,
        app: AppConfig | None = None,
    ) -> list[IntentWorkUnit]:
        phase = state.phase
        if phase not in {
            PairedBinaryPhase.STOP_PENDING_YES,
            PairedBinaryPhase.STOP_PENDING_NO,
            PairedBinaryPhase.TP_PENDING_YES,
            PairedBinaryPhase.TP_PENDING_NO,
            PairedBinaryPhase.TIMEOUT_PENDING,
        }:
            return []

        pair_id = state.pair_correlation_id or "paired_binary_unknown"
        legs: list[tuple[str, Decimal, str | None]] = []
        retry_decision_id: str | None = None

        if phase == PairedBinaryPhase.TIMEOUT_PENDING:
            for leg in state.pending_timeout_legs or ("yes", "no"):
                rt = state.yes if leg == "yes" else state.no
                if rt.exit_submitted:
                    continue
                bid = yes_book.bid if leg == "yes" else no_book.bid
                legs.append((leg, bid, state.yes_target if leg == "yes" else state.no_target))
        else:
            leg = self._pending_leg_from_phase(phase)
            rt = state.yes if leg == "yes" else state.no
            if rt.exit_submitted:
                return []
            bid = yes_book.bid if leg == "yes" else no_book.bid
            trigger = rt.pending_trigger_type or "stop_loss"
            legs.append((leg, bid, state.yes_target if leg == "yes" else state.no_target))
            if sink and run_id and rt.pending_trigger_reason:
                pb_facts.emit_exit_retry(
                    sink,
                    run_id,
                    state,
                    yes_book,
                    no_book,
                    leg=leg,
                    trigger_type=trigger,
                    reason=rt.pending_trigger_reason,
                )
                retry_decision_id = self._emit_trigger_snapshot(
                    app=app,
                    coord=coord,
                    state=state,
                    sink=sink,
                    run_id=run_id,
                    decision_type="fak_retry",
                    trigger_type=trigger,
                    is_retry=True,
                )

        work: list[IntentWorkUnit] = []
        for leg, bid, target in legs:
            rt = state.yes if leg == "yes" else state.no
            trigger_type = rt.pending_trigger_type or "stop_loss"
            did = retry_decision_id if rt.pending_trigger_reason else None
            work.extend(
                self._dispatch_exit(
                    coord,
                    state,
                    leg=leg,  # type: ignore[arg-type]
                    trigger_type=trigger_type,  # type: ignore[arg-type]
                    bid=bid,
                    qty=state.effective_qty,
                    pair_id=pair_id,
                    target_price=target,
                    yes_book=yes_book,
                    no_book=no_book,
                    sink=sink,
                    run_id=run_id,
                    app=app,
                    decision_id=did,
                    is_retry=bool(rt.pending_trigger_reason),
                    append=True,
                )
            )
        return work

    @staticmethod
    def _pending_leg_from_phase(phase: PairedBinaryPhase) -> str:
        if phase in {PairedBinaryPhase.STOP_PENDING_YES, PairedBinaryPhase.TP_PENDING_YES}:
            return "yes"
        return "no"

    def _dispatch_exit(
        self,
        coord: RuntimeCoordinator,
        state: PairedBinaryRuntimeState,
        *,
        leg: str,
        trigger_type: str,
        bid: Decimal,
        qty: Decimal,
        pair_id: str,
        target_price: Decimal | None,
        yes_book: LegBook,
        no_book: LegBook,
        sink: JsonlSink | None,
        run_id: RunId | None,
        app: AppConfig | None = None,
        decision_id: str | None = None,
        is_retry: bool = False,
        append: bool = False,
    ) -> list[IntentWorkUnit]:
        if app is not None:
            ctx = trigger_to_context(trigger_type)
            gate_result = should_block_paired_binary_decision(
                app=app,
                coord=coord,
                cfg=self._cfg,
                context=ctx,
                size=qty,
            )
            if not gate_result.allowed:
                if sink and run_id:
                    emit_market_data_health_block(
                        sink,
                        run_id,
                        block_reason=gate_result.block_reason or "decision_blocked",
                        decision_context=ctx.value,
                        readiness_state=gate_result.readiness_state,
                        quality_verdict=gate_result.quality_verdict,
                        quality_reasons=gate_result.quality_reasons,
                        correlation_id=state.pair_correlation_id,
                    )
                    if gate_result.quality_verdict == "reject_decision" and trigger_type in (
                        "timeout",
                        "stop_loss",
                        "stop",
                    ):
                        pb_facts.emit_manual_intervention_required(
                            sink,
                            run_id,
                            state,
                            yes_book,
                            no_book,
                            attempt_count=0,
                            reason="exit_book_age_reject",
                        )
                return []

        result = try_build_exit(
            coord,
            self._cfg,
            state,
            leg=leg,  # type: ignore[arg-type]
            trigger_type=trigger_type,  # type: ignore[arg-type]
            bid=bid,
            qty=qty,
            pair_id=pair_id,
            book_stale=False,
            target_price=target_price,
        )

        if result.blocked and result.ctx.reason:
            record_exit_blocked(
                state,
                leg,  # type: ignore[arg-type]
                reason=result.ctx.reason,
                trigger_type=trigger_type,  # type: ignore[arg-type]
            )
            if sink and run_id:
                pb_facts.emit_exit_trigger_pending(
                    sink,
                    run_id,
                    state,
                    yes_book,
                    no_book,
                    leg=leg,
                    trigger_type=trigger_type,
                    ctx=result.ctx,
                )
                pb_facts.emit_exit_submit_blocked(
                    sink,
                    run_id,
                    state,
                    yes_book,
                    no_book,
                    leg=leg,
                    trigger_type=trigger_type,
                    ctx=result.ctx,
                )
            return []

        if result.work is None:
            return []

        if sink and run_id:
            pb_facts.emit_exit_submit_attempt(
                sink,
                run_id,
                state,
                yes_book,
                no_book,
                leg=leg,
                trigger_type=trigger_type,
                trigger_price=bid,
                reference_price=result.ctx.reference_price,
                target_price=target_price,
                planned_qty=qty,
                allocation_qty=result.ctx.allocation_qty,
                venue_available_qty=result.ctx.venue_available_qty,
                final_size=result.ctx.final_size,
            )

        did = decision_id
        if app is not None and sink is not None and run_id is not None and did is None and not is_retry:
            snap_type = "timeout_exit" if trigger_type == "timeout" else None
            if snap_type is not None:
                did = self._emit_trigger_snapshot(
                    app=app,
                    coord=coord,
                    state=state,
                    sink=sink,
                    run_id=run_id,
                    decision_type=snap_type,
                    trigger_type=trigger_type,
                )
        did = did or new_decision_id()
        extensions = dict(result.work.intent_fact_extensions or {})
        extensions.update(
            {
                "decision_id": did,
                "decision_type": "fak_retry" if is_retry else "exit_submit",
                "trigger_type": trigger_type,
                "pre_decision_snapshot_emitted": decision_id is not None or is_retry or trigger_type == "timeout",
            }
        )
        work_unit = replace(result.work, intent_fact_extensions=extensions)
        return [work_unit]
