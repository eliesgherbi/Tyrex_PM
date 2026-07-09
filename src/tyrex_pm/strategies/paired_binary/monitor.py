"""Paired binary monitor — emits ExitIntent work units only (Phase 4.6)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from tyrex_pm.core.enums import OrderStyle, Side
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
    prepare_survival_enforce_trigger,
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
        event_correlation: pb_facts.EventCorrelationContext | None = None,
    ) -> str | None:
        if app is None or sink is None or run_id is None:
            return None
        dtype = "fak_retry" if is_retry else decision_type
        corr = event_correlation or getattr(self, "_tick_event_correlation", None)
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
            event_correlation=corr,
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
        monitor_trigger: str = "poll",
        event_correlation: pb_facts.EventCorrelationContext | None = None,
    ) -> list[IntentWorkUnit]:
        if app is not None and app.runtime.observability.emit_event_correlation:
            state._event_correlation_context = event_correlation
            state._emit_event_correlation = True
        else:
            state._event_correlation_context = None
            state._emit_event_correlation = False
        self._tick_event_correlation = event_correlation
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

        resting_work = self._maybe_manage_survival_resting_orders(
            coord, state, yes_book, no_book, sink, run_id, app=app
        )
        if resting_work:
            return resting_work

        quality_retry_work = self._retry_pending_survival_quality_exit(
            coord, state, yes_book, no_book, sink, run_id, app=app
        )
        if quality_retry_work:
            return quality_retry_work

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
            survival_active = app is not None and app.survival.enabled
            if (
                not survival_active
                and state.yes_target is not None
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
            return self._evaluate_survival_advisory(
                app=app,
                coord=coord,
                state=state,
                yes_book=yes_book,
                no_book=no_book,
                sink=sink,
                run_id=run_id,
                pair_id=pair_id,
                monitor_trigger=monitor_trigger,
            )

        if state.phase == PairedBinaryPhase.ONLY_NO_ACTIVE:
            survival_active = app is not None and app.survival.enabled
            if (
                not survival_active
                and state.no_target is not None
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
            return self._evaluate_survival_advisory(
                app=app,
                coord=coord,
                state=state,
                yes_book=yes_book,
                no_book=no_book,
                sink=sink,
                run_id=run_id,
                pair_id=pair_id,
                monitor_trigger=monitor_trigger,
            )

        return work

    def _evaluate_survival_advisory(
        self,
        *,
        app: AppConfig | None,
        coord: RuntimeCoordinator,
        state: PairedBinaryRuntimeState,
        yes_book: LegBook,
        no_book: LegBook,
        sink: JsonlSink | None,
        run_id: RunId | None,
        pair_id: str,
        monitor_trigger: str = "poll",
    ) -> list[IntentWorkUnit]:
        if app is None or not app.survival.enabled:
            return []
        mode = app.survival.monitor_mode
        if mode == "ws_event" and monitor_trigger != "ws_book_update":
            return []
        flatten = 20.0
        if app.runtime.strategy_lifecycle.enabled:
            flatten = app.runtime.strategy_lifecycle.flatten_before_event_end_s
        from tyrex_pm.survival.advisory import evaluate_survival_advisory

        result = evaluate_survival_advisory(
            app=app,
            coord=coord,
            state=state,
            cfg=self._cfg,
            yes_book=yes_book,
            no_book=no_book,
            sink=sink,
            run_id=run_id,
            flatten_before_event_end_s=flatten,
            monitor_trigger=monitor_trigger,
        )
        return self._maybe_dispatch_survival_enforcement(
            app=app,
            coord=coord,
            state=state,
            yes_book=yes_book,
            no_book=no_book,
            sink=sink,
            run_id=run_id,
            pair_id=pair_id,
            result=result,
            flatten_before_event_end_s=flatten,
        )

    def _maybe_dispatch_survival_enforcement(
        self,
        *,
        app: AppConfig,
        coord: RuntimeCoordinator,
        state: PairedBinaryRuntimeState,
        yes_book: LegBook,
        no_book: LegBook,
        sink: JsonlSink | None,
        run_id: RunId | None,
        pair_id: str,
        result,
        flatten_before_event_end_s: float,
    ) -> list[IntentWorkUnit]:
        from tyrex_pm.survival.enforcement_dispatch import (
            abandon_pending_survival_exit_intent,
            build_enforce_exit_payload,
            build_survival_order_extensions,
            enforce_module_from_result,
            enforce_trigger_from_reason,
            executable_bid_for_dispatch,
            latch_pending_survival_exit_intent,
            mark_enforce_exit_pending,
            pending_intent_retry_payload,
            select_enforce_order_decision,
            should_skip_survival_enforce,
            survivor_leg_from_state,
        )
        from tyrex_pm.survival.order_policy import order_decision_payload
        from tyrex_pm.survival.exit_planning import planner_from_app

        survivor_leg = survivor_leg_from_state(state)
        if survivor_leg is None:
            return []

        module = enforce_module_from_result(result)
        if module is None:
            return []

        seconds_to_close = None
        pb = app.paired_binary
        if pb is not None and pb.event_end_ts is not None:
            import time

            seconds_to_close = float(pb.event_end_ts) - time.time()

        if result.enforce_stall_downgrade:
            return []

        if not result.enforce_exit:
            return []

        skip, skip_reason = should_skip_survival_enforce(
            state,
            survivor_leg=survivor_leg,
            seconds_to_close=seconds_to_close,
            flatten_before_event_end_s=flatten_before_event_end_s,
        )
        enforcement_mode = "advisory"
        if module == "trailing_stop":
            enforcement_mode = app.survival.trailing_stop.enforcement_mode
        elif module == "survivor_floor":
            enforcement_mode = app.survival.survivor_floor.enforcement_mode
        elif module == "economics":
            enforcement_mode = app.survival.economics.enforcement_mode

        payload = build_enforce_exit_payload(
            module=module,
            reason=result.enforce_exit_reason,
            survivor_leg=survivor_leg,
            result=result,
            enforcement_mode=enforcement_mode,
            existing_exit_pending=state.phase in {
                PairedBinaryPhase.TP_PENDING_YES,
                PairedBinaryPhase.TP_PENDING_NO,
                PairedBinaryPhase.STOP_PENDING_YES,
                PairedBinaryPhase.STOP_PENDING_NO,
            },
            skip_reason=skip_reason,
            target_mode=(state.survivor_leg_state or {}).get("selected_mode"),
        )
        if app.runtime.observability.emit_event_correlation:
            payload.update(
                pb_facts.build_event_correlation_fields(
                    getattr(state, "_event_correlation_context", None),
                    enabled=True,
                )
            )
        if sink and run_id:
            pb_facts.emit_survival_enforce_exit_requested(
                sink, run_id, state, yes_book, no_book, payload=payload
            )
        if skip:
            if skip_reason == "pre_close_flatten_preempts":
                ctx = abandon_pending_survival_exit_intent(
                    state, reason="pre_close_flatten_preempted"
                )
                if ctx.get("module") and sink and run_id:
                    pb_facts.emit_survival_enforce_exit_abandoned(
                        sink,
                        run_id,
                        state,
                        yes_book,
                        no_book,
                        payload={
                            "reason": "pre_close_flatten_preempted",
                            "module": ctx.get("module"),
                            "trigger_type": ctx.get("trigger_type"),
                            "survivor_leg": survivor_leg,
                            "attempt_count": ctx.get("attempt_count"),
                        },
                    )
            if sink and run_id:
                pb_facts.emit_survival_enforce_exit_skipped(
                    sink, run_id, state, yes_book, no_book, payload=payload
                )
            return []

        exit_eval = result.exit_eval
        if exit_eval is None or exit_eval.verdict in {"defer", "blocked"}:
            skip_reason = exit_eval.reason if exit_eval else "missing_executable_evidence"
            payload["skip_reason"] = skip_reason
            ev = exit_eval.evidence if exit_eval else None
            quality_detail = ev.quality_reject_detail if ev else None
            if quality_detail is not None:
                payload["quality_reject_detail"] = quality_detail
            if ev is not None:
                payload["book_age_ms"] = ev.book_age_ms
                payload["spread"] = str(ev.spread) if ev.spread is not None else None
                payload["depth_fraction"] = str(ev.available_depth_fraction)
            enf_cfg = app.survival.enforcement
            retryable = skip_reason == "quality_reject" and enf_cfg.retry_quality_rejects
            payload["retryable"] = retryable
            payload["latched_intent"] = False
            if retryable:
                trigger_type = enforce_trigger_from_reason(result.enforce_exit_reason)
                latch_pending_survival_exit_intent(
                    state,
                    module=module,
                    reason=result.enforce_exit_reason,
                    trigger_type=trigger_type,
                    skip_reason=skip_reason,
                    quality_reject_detail=quality_detail,
                )
                payload["latched_intent"] = True
                payload.update(
                    pending_intent_retry_payload(
                        state,
                        seconds_to_close=seconds_to_close,
                        backoff_s=enf_cfg.quality_reject_retry_backoff_s,
                    )
                )
                payload["trigger_type"] = trigger_type
                if sink and run_id:
                    pb_facts.emit_survival_enforce_exit_retry_scheduled(
                        sink, run_id, state, yes_book, no_book, payload=payload
                    )
            if sink and run_id:
                pb_facts.emit_survival_enforce_exit_skipped(
                    sink, run_id, state, yes_book, no_book, payload=payload
                )
            return []

        planner = planner_from_app(app)
        bid = executable_bid_for_dispatch(result, planner)
        if bid is None:
            payload["skip_reason"] = "no_executable_bid"
            if sink and run_id:
                pb_facts.emit_survival_enforce_exit_skipped(
                    sink, run_id, state, yes_book, no_book, payload=payload
                )
            return []

        trigger_type = enforce_trigger_from_reason(result.enforce_exit_reason)
        qty = exit_eval.recommended_qty if exit_eval.recommended_qty > 0 else state.effective_qty
        target_price = state.yes_target if survivor_leg == "yes" else state.no_target

        allow_partial = bool(app.survival.enforcement.order_policy.allow_partial_retry)
        order_dec = select_enforce_order_decision(
            app,
            state,
            survivor_leg=survivor_leg,
            qty=qty,
            touch_price=bid,
            executable_price=bid,
            seconds_to_close=seconds_to_close,
            allow_partial=allow_partial,
            trigger_type=trigger_type,
            module=module,
            is_retry=False,
        )
        if order_dec is None:
            payload["skip_reason"] = "order_policy_abandoned"
            if sink and run_id:
                pb_facts.emit_survival_enforce_exit_skipped(
                    sink, run_id, state, yes_book, no_book, payload=payload
                )
                pb_facts.emit_survival_exit_order_policy_abandoned(
                    sink,
                    run_id,
                    state,
                    yes_book,
                    no_book,
                    payload=payload,
                )
            return []

        if sink and run_id:
            pb_facts.emit_survival_exit_order_type_selected(
                sink,
                run_id,
                state,
                yes_book,
                no_book,
                payload={
                    **payload,
                    **order_decision_payload(
                        order_dec,
                        module=module,
                        trigger_type=trigger_type,
                        survivor_leg=survivor_leg,
                        side=Side.SELL,
                        time_to_close=seconds_to_close,
                    ),
                },
            )

        prepare_survival_enforce_trigger(state, survivor_leg, trigger_type)  # type: ignore[arg-type]
        mark_enforce_exit_pending(state, module=module)
        decision_id = self._emit_trigger_snapshot(
            app=app,
            coord=coord,
            state=state,
            sink=sink,
            run_id=run_id,
            decision_type="survival_enforce_exit",
            trigger_type=trigger_type,
        )
        work = self._dispatch_exit(
            coord,
            state,
            leg=survivor_leg,
            trigger_type=trigger_type,
            bid=bid,
            qty=qty,
            pair_id=pair_id,
            target_price=target_price,
            yes_book=yes_book,
            no_book=no_book,
            sink=sink,
            run_id=run_id,
            app=app,
            decision_id=decision_id,
            order_style=order_dec.order_style,
            limit_price=order_dec.price,
            urgency=order_dec.urgency,
            extra_extensions=build_survival_order_extensions(
                module=module,
                trigger_type=trigger_type,
                decision=order_dec,
                survivor_leg=survivor_leg,
            ),
        )
        if work:
            if sink and run_id:
                submitted = dict(payload)
                submitted["trigger_type"] = trigger_type
                submitted["planned_qty"] = str(qty)
                pb_facts.emit_survival_enforce_exit_submitted(
                    sink, run_id, state, yes_book, no_book, payload=submitted
                )
        elif sink and run_id:
            payload["skip_reason"] = "dispatch_blocked"
            pb_facts.emit_survival_enforce_exit_skipped(
                sink, run_id, state, yes_book, no_book, payload=payload
            )
        return work

    def _retry_pending_survival_quality_exit(
        self,
        coord: RuntimeCoordinator,
        state: PairedBinaryRuntimeState,
        yes_book: LegBook,
        no_book: LegBook,
        sink: JsonlSink | None,
        run_id: RunId | None,
        *,
        app: AppConfig | None,
    ) -> list[IntentWorkUnit]:
        import time

        from tyrex_pm.market_data.quality import DecisionContext
        from tyrex_pm.survival.enforcement_dispatch import (
            abandon_pending_survival_exit_intent,
            abandon_reason_for_pending_survival_exit,
            has_pending_survival_exit_intent,
            pending_survival_exit_context,
            record_pending_survival_exit_retry_attempt,
            should_retry_pending_survival_exit,
        )
        from tyrex_pm.survival.exit_planning import planner_from_app
        from tyrex_pm.survival.models import SurvivalAdvisoryResult

        if app is None or not has_pending_survival_exit_intent(state):
            return []
        if state.phase not in {
            PairedBinaryPhase.ONLY_YES_ACTIVE,
            PairedBinaryPhase.ONLY_NO_ACTIVE,
        }:
            return []

        enf_cfg = app.survival.enforcement
        now = time.time()
        abandon = abandon_reason_for_pending_survival_exit(state, enf_cfg, now=now)
        if abandon:
            ctx = abandon_pending_survival_exit_intent(state, reason=abandon)
            if sink and run_id:
                survivor_leg = "yes" if state.phase == PairedBinaryPhase.ONLY_YES_ACTIVE else "no"
                pb_facts.emit_survival_enforce_exit_abandoned(
                    sink,
                    run_id,
                    state,
                    yes_book,
                    no_book,
                    payload={
                        "reason": abandon,
                        "module": ctx.get("module"),
                        "trigger_type": ctx.get("trigger_type"),
                        "survivor_leg": survivor_leg,
                        "attempt_count": ctx.get("attempt_count"),
                        "quality_reject_detail": ctx.get("quality_reject_detail"),
                    },
                )
            return []

        ok, block_reason = should_retry_pending_survival_exit(state, enf_cfg, now=now)
        if not ok:
            return []

        ctx = pending_survival_exit_context(state)
        survivor_leg = "yes" if state.phase == PairedBinaryPhase.ONLY_YES_ACTIVE else "no"
        token_id = self._cfg.yes_token_id if survivor_leg == "yes" else self._cfg.no_token_id
        attempt_count = record_pending_survival_exit_retry_attempt(state)

        seconds_to_close = None
        pb = app.paired_binary
        if pb is not None and pb.event_end_ts is not None:
            seconds_to_close = float(pb.event_end_ts) - now

        planner = planner_from_app(app)
        exit_eval = planner.evaluate_exit(
            coord=coord,
            token_id=token_id,
            qty=state.effective_qty,
            decision_context=DecisionContext.URGENT_EXIT,
        )
        ev = exit_eval.evidence
        retry_payload: dict[str, object] = {
            "reason": "quality_reject",
            "module": ctx.get("module"),
            "trigger_type": ctx.get("trigger_type"),
            "survivor_leg": survivor_leg,
            "attempt_count": attempt_count,
            "book_age_ms": ev.book_age_ms,
            "spread": str(ev.spread) if ev.spread is not None else None,
            "depth_fraction": str(ev.available_depth_fraction),
            "seconds_to_close": seconds_to_close,
            "next_retry_ts": now + enf_cfg.quality_reject_retry_backoff_s,
            "quality_reject_detail": ev.quality_reject_detail or ctx.get("quality_reject_detail"),
        }
        if exit_eval.verdict in {"defer", "blocked"}:
            retry_payload["retry_outcome"] = "still_rejected"
            retry_payload["skip_reason"] = exit_eval.reason
            if sink and run_id:
                pb_facts.emit_survival_enforce_exit_retry_attempted(
                    sink, run_id, state, yes_book, no_book, payload=retry_payload
                )
            return []

        retry_payload["retry_outcome"] = "quality_passed"
        if sink and run_id:
            pb_facts.emit_survival_enforce_exit_retry_attempted(
                sink, run_id, state, yes_book, no_book, payload=retry_payload
            )

        module = str(ctx.get("module") or "trailing_stop")
        result = SurvivalAdvisoryResult(
            exit_eval=exit_eval,
            reachability=None,
            stall=None,
            trailing=None,
            economics=None,
            enforce_exit=True,
            enforce_exit_reason=ctx.get("reason"),
            enforce_module=module,
        )
        flatten_before = 20.0
        if app.runtime.strategy_lifecycle.enabled:
            flatten_before = app.runtime.strategy_lifecycle.flatten_before_event_end_s
        pair_id = state.pair_correlation_id or "paired_binary_unknown"
        return self._maybe_dispatch_survival_enforcement(
            app=app,
            coord=coord,
            state=state,
            yes_book=yes_book,
            no_book=no_book,
            sink=sink,
            run_id=run_id,
            pair_id=pair_id,
            result=result,
            flatten_before_event_end_s=flatten_before,
        )

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

    def _maybe_manage_survival_resting_orders(
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
        import time

        from tyrex_pm.survival.enforcement_dispatch import (
            build_resting_cancel_work,
            resting_order_open,
            resting_ttl_expired,
            survivor_leg_from_state,
        )
        from tyrex_pm.survival.order_policy import SurvivalRestingState

        if not resting_order_open(state):
            return []

        raw = state.survivor_leg_state or {}
        if raw.get("survival_resting_state") == SurvivalRestingState.RESTING_CANCEL_REQUESTED.value:
            return []

        leg = survivor_leg_from_state(state)
        if leg is None and state.phase in {
            PairedBinaryPhase.TP_PENDING_YES,
            PairedBinaryPhase.STOP_PENDING_YES,
        }:
            leg = "yes"
        elif leg is None and state.phase in {
            PairedBinaryPhase.TP_PENDING_NO,
            PairedBinaryPhase.STOP_PENDING_NO,
        }:
            leg = "no"
        if leg is None:
            return []

        venue_order_id = raw.get("survival_resting_order_id")
        if not venue_order_id:
            return []

        token_id = self._cfg.yes_token_id if leg == "yes" else self._cfg.no_token_id
        pair_id = state.pair_correlation_id or "paired_binary_unknown"
        cancel_reason: str | None = None
        seconds_to_close: float | None = None

        if resting_ttl_expired(state):
            cancel_reason = "local_ttl_expired"
        elif app is not None:
            pb = app.paired_binary
            if pb is not None and pb.event_end_ts is not None:
                seconds_to_close = float(pb.event_end_ts) - time.time()
            flatten = 20.0
            if app.runtime.strategy_lifecycle.enabled:
                flatten = app.runtime.strategy_lifecycle.flatten_before_event_end_s
            if seconds_to_close is not None and seconds_to_close <= flatten:
                cancel_reason = "pre_close_flatten"

        if cancel_reason is None:
            return []

        if sink and run_id:
            pb_facts.emit_survival_exit_resting_order_cancel_requested(
                sink,
                run_id,
                state,
                yes_book,
                no_book,
                payload={
                    "module": raw.get("enforce_module", "trailing_stop"),
                    "trigger_type": raw.get("survival_trigger_type", "survival_trailing_stop"),
                    "survivor_leg": leg,
                    "order_id": str(venue_order_id),
                    "cancel_order_id": str(venue_order_id),
                    "reason": cancel_reason,
                    "time_to_close": seconds_to_close,
                },
            )

        work = build_resting_cancel_work(
            state,
            token_id=str(token_id),
            leg=str(leg),
            pair_id=pair_id,
            venue_order_id=str(venue_order_id),
        )
        return [work] if work is not None else []

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

        from tyrex_pm.survival.enforcement_dispatch import (
            build_survival_order_extensions,
            mark_enforce_in_flight,
            select_enforce_order_decision,
        )
        from tyrex_pm.survival.order_policy import order_decision_payload

        raw = state.survivor_leg_state or {}
        if raw.get("enforce_exit_in_flight"):
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
            order_style = None
            limit_price = None
            urgency = None
            extra_ext = None
            if str(trigger_type).startswith("survival_") and app is not None:
                module = (state.survivor_leg_state or {}).get("enforce_module", "trailing_stop")
                seconds_to_close = None
                pb_cfg = app.paired_binary
                if pb_cfg is not None and pb_cfg.event_end_ts is not None:
                    import time

                    seconds_to_close = float(pb_cfg.event_end_ts) - time.time()
                order_dec = select_enforce_order_decision(
                    app,
                    state,
                    survivor_leg=leg,
                    qty=state.effective_qty,
                    touch_price=bid,
                    executable_price=bid,
                    seconds_to_close=seconds_to_close,
                    allow_partial=bool(app.survival.enforcement.order_policy.allow_partial_retry),
                    trigger_type=str(trigger_type),
                    module=str(module),
                    is_retry=True,
                )
                if order_dec is None:
                    continue
                mark_enforce_in_flight(state, module=str(module))
                order_style = order_dec.order_style
                limit_price = order_dec.price
                urgency = order_dec.urgency
                extra_ext = build_survival_order_extensions(
                    module=str(module),
                    trigger_type=str(trigger_type),
                    decision=order_dec,
                    survivor_leg=leg,
                )
                if sink and run_id:
                    pb_facts.emit_survival_exit_order_repriced(
                        sink,
                        run_id,
                        state,
                        yes_book,
                        no_book,
                        payload=order_decision_payload(
                            order_dec,
                            module=str(module),
                            trigger_type=str(trigger_type),
                            survivor_leg=leg,
                            side=Side.SELL,
                            time_to_close=seconds_to_close,
                        ),
                    )
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
                    order_style=order_style,
                    limit_price=limit_price,
                    urgency=urgency,
                    extra_extensions=extra_ext,
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
        order_style: OrderStyle | None = None,
        limit_price: Decimal | None = None,
        urgency: str | None = None,
        extra_extensions: dict[str, object] | None = None,
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
                        "survival_trailing_stop",
                        "survival_stall_exit",
                        "survival_economics_exit",
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
            order_style=order_style,
            limit_price=limit_price,
            urgency=urgency,
            extra_extensions=extra_extensions,
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
