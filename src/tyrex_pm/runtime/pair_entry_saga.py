"""Compound pair-entry coordinator (PairEntrySaga).

Orchestrates multi-leg entry with pair-level preflight, explicit execution-style
policy, resting-order cancellation, and shared emergency-unwind retry cleanup.
Each leg still flows through ``process_intent_work_unit`` (risk → planner → OMS).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Awaitable, Callable

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId, VenueOrderId
from tyrex_pm.core.models import CancelIntent, EnterIntent, IntentId
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.execution.planner import ExecutionPlanner
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_OMS_CANCEL, FACT_TYPE_OMS_REJECT
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.risk.engine import evaluate_intent
from tyrex_pm.risk.planned_order import validate_planned_order
from tyrex_pm.risk.pretrade import estimate_notional
from tyrex_pm.runtime.config import AppConfig, PairedBinaryStrategyConfig
from tyrex_pm.runtime.intent_work import IntentWorkUnit
from tyrex_pm.runtime.pipeline import process_intent_work_unit
from tyrex_pm.state.entry_fill_lifecycle import classify_ack_status, is_resting_ack, resolve_leg_fill_snapshot
from tyrex_pm.strategies.base import StrategyContext
from tyrex_pm.strategies.paired_binary import facts as pb_facts
from tyrex_pm.strategies.paired_binary.emergency_unwind import EmergencyUnwindOutcome
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook
from tyrex_pm.strategies.paired_binary.lifecycle import resolve_min_effective_pair_qty, transition_phase
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from tyrex_pm.strategies.paired_binary.strategy import PairedBinaryStrategy

log = logging.getLogger(__name__)


class PairEntrySagaPhase(str, Enum):
    PAIR_PREFLIGHTED = "PAIR_PREFLIGHTED"
    PAIR_SUBMITTING = "PAIR_SUBMITTING"
    PAIR_SUBMITTED = "PAIR_SUBMITTED"
    PAIR_PARTIALLY_FILLED = "PAIR_PARTIALLY_FILLED"
    PAIR_RESTING = "PAIR_RESTING"
    PAIR_COMMITTED = "PAIR_COMMITTED"
    PAIR_ABORTING = "PAIR_ABORTING"
    PAIR_ABORTED_FLAT = "PAIR_ABORTED_FLAT"
    PAIR_ABORTED_MANUAL_INTERVENTION = "PAIR_ABORTED_MANUAL_INTERVENTION"


UnwindFn = Callable[..., Awaitable[EmergencyUnwindOutcome]]


@dataclass
class LegPreflightResult:
    leg: str
    approved: bool
    risk_approved: bool
    planner_approved: bool
    planned_valid: bool
    reason_codes: tuple[str, ...] = ()
    requested_style: str | None = None
    planned_style: str | None = None
    planned_limit_price: Decimal | None = None
    notional_usd: Decimal = Decimal("0")
    planner_reason: str | None = None


@dataclass
class PairPreflightResult:
    ok: bool
    yes: LegPreflightResult
    no: LegPreflightResult
    pair_notional: Decimal
    reason_codes: tuple[str, ...] = ()


@dataclass
class LegSubmitOutcome:
    leg: str
    submitted: bool
    matched: bool
    resting: bool
    rejected: bool
    risk_denied: bool
    client_order_id: str | None = None
    venue_order_id: str | None = None
    ack_status: str | None = None
    filled_qty: Decimal = Decimal("0")
    requested_style: str | None = None
    planned_style: str | None = None
    style_mismatch: bool = False
    reason_codes: tuple[str, ...] = ()


@dataclass
class PairEntryAbortOutcome:
    flat: bool
    manual_intervention: bool
    reason: str


def _leg_notional(intent: EnterIntent) -> Decimal:
    return estimate_notional(intent)


def preflight_leg(
    intent: EnterIntent,
    *,
    leg: str,
    app: AppConfig,
    coord,
    run_id: RunId,
) -> LegPreflightResult:
    """Dry-run risk → planner → validate_planned_order for one leg (no OMS)."""
    risk_ctx = coord.build_risk_context(app)
    requested_style = intent.order_style.value
    notional = _leg_notional(intent)
    decision = evaluate_intent(intent, risk_ctx, app=app, run_id=run_id)
    if not decision.approved:
        return LegPreflightResult(
            leg=leg,
            approved=False,
            risk_approved=False,
            planner_approved=False,
            planned_valid=False,
            reason_codes=tuple(decision.reason_codes),
            requested_style=requested_style,
            notional_usd=notional,
        )
    ap = decision.approved_intent
    assert ap is not None
    planner_approved = True
    planned_valid = True
    planned_style: str | None = None
    planned_limit: Decimal | None = None
    planner_reason: str | None = None
    reason_codes: list[str] = list(decision.reason_codes)
    if app.execution.planner.enabled:
        planner = ExecutionPlanner(app.execution.planner)
        result = planner.plan(ap, market_state=coord.market_state)
        if not result.approved or result.plan is None:
            return LegPreflightResult(
                leg=leg,
                approved=False,
                risk_approved=True,
                planner_approved=False,
                planned_valid=False,
                reason_codes=(result.reason or "planner_denied",),
                requested_style=requested_style,
                notional_usd=notional,
                planner_reason=result.reason,
            )
        final = validate_planned_order(result.plan, risk_ctx, app=app)
        if not final.approved or final.approved_intent is None:
            return LegPreflightResult(
                leg=leg,
                approved=False,
                risk_approved=True,
                planner_approved=True,
                planned_valid=False,
                reason_codes=tuple(final.reason_codes),
                requested_style=requested_style,
                notional_usd=notional,
                planner_reason=result.reason,
            )
        planned_style = final.approved_intent.intent.order_style.value
        planned_limit = final.approved_intent.intent.limit_price
        planner_reason = result.reason
        reason_codes = list(final.reason_codes)
    else:
        planned_style = requested_style
        planned_limit = intent.limit_price
    return LegPreflightResult(
        leg=leg,
        approved=True,
        risk_approved=True,
        planner_approved=planner_approved,
        planned_valid=planned_valid,
        reason_codes=tuple(reason_codes),
        requested_style=requested_style,
        planned_style=planned_style,
        planned_limit_price=planned_limit,
        notional_usd=notional,
        planner_reason=planner_reason,
    )


def preflight_pair_entry(
    pairs: list[tuple[EnterIntent, dict]],
    *,
    app: AppConfig,
    coord,
    run_id: RunId,
    cfg: PairedBinaryStrategyConfig,
) -> PairPreflightResult:
    yes_intent, yes_ext = pairs[0]
    no_intent, no_ext = pairs[1]
    yes_pf = preflight_leg(yes_intent, leg="yes", app=app, coord=coord, run_id=run_id)
    no_pf = preflight_leg(no_intent, leg="no", app=app, coord=coord, run_id=run_id)
    pair_notional = yes_pf.notional_usd + no_pf.notional_usd
    reason_codes: list[str] = []
    if not yes_pf.approved:
        reason_codes.extend(yes_pf.reason_codes)
    if not no_pf.approved:
        reason_codes.extend(no_pf.reason_codes)
    def _reject_style_mismatch(pf: LegPreflightResult) -> LegPreflightResult:
        if (
            pf.approved
            and pf.requested_style
            and pf.planned_style
            and pf.requested_style != pf.planned_style
            and not cfg.allow_entry_style_downgrade
        ):
            reason_codes.append("entry_style_mismatch")
            return LegPreflightResult(
                leg=pf.leg,
                approved=False,
                risk_approved=pf.risk_approved,
                planner_approved=pf.planner_approved,
                planned_valid=pf.planned_valid,
                reason_codes=(*pf.reason_codes, "entry_style_mismatch"),
                requested_style=pf.requested_style,
                planned_style=pf.planned_style,
                planned_limit_price=pf.planned_limit_price,
                notional_usd=pf.notional_usd,
                planner_reason=pf.planner_reason,
            )
        return pf

    yes_pf = _reject_style_mismatch(yes_pf)
    no_pf = _reject_style_mismatch(no_pf)
    ok = yes_pf.approved and no_pf.approved
    return PairPreflightResult(
        ok=ok,
        yes=yes_pf,
        no=no_pf,
        pair_notional=pair_notional,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
    )


def _inspect_leg_after_submit(
    coord,
    cfg: PairedBinaryStrategyConfig,
    *,
    leg: str,
    client_order_id: str | None,
    requested_style: str | None,
    planned_style: str | None,
) -> LegSubmitOutcome:
    token_id = TokenId(cfg.yes_token_id if leg == "yes" else cfg.no_token_id)
    snap = resolve_leg_fill_snapshot(
        coord,
        token_id=token_id,
        owner_id=cfg.owner_id,
        client_order_id=client_order_id,
    )
    lo = coord.orders.orders.get(ClientOrderId(client_order_id)) if client_order_id else None
    ack = lo.ack_status if lo is not None else None
    resting = is_resting_ack({"match_status": ack or ""}) if ack else snap.status.value in {
        "RESTING",
        "SUBMITTED",
    }
    matched = snap.filled_qty > 0 or (lo is not None and lo.size_matched and lo.size_matched > 0)
    if matched:
        resting = False
    style_mismatch = bool(
        requested_style and planned_style and requested_style != planned_style
    )
    return LegSubmitOutcome(
        leg=leg,
        submitted=client_order_id is not None,
        matched=matched,
        resting=resting and not matched,
        rejected=False,
        risk_denied=client_order_id is None,
        client_order_id=client_order_id,
        venue_order_id=str(lo.venue_order_id) if lo and lo.venue_order_id else None,
        ack_status=ack or snap.status.value,
        filled_qty=snap.filled_qty,
        requested_style=requested_style,
        planned_style=planned_style,
        style_mismatch=style_mismatch,
    )


async def cancel_resting_entry_leg(
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
    leg: str,
    reason: str,
    apply_local_shadow_fill: bool,
    live_clob_client,
    pair_correlation_id: str,
) -> bool:
    """Cancel an open entry order via CancelIntent + pipeline. Returns True if cancelled or absent."""
    leg_state = state.yes if leg == "yes" else state.no
    cid = leg_state.entry_client_order_id
    if not cid:
        return True
    lo = coord.orders.orders.get(ClientOrderId(cid))
    if lo is None or lo.remaining <= 0:
        return True
    vid = lo.venue_order_id
    pb_facts.emit_entry_cancel_attempt(
        sink,
        run_id,
        state,
        yes_book,
        no_book,
        leg=leg,
        client_order_id=cid,
        venue_order_id=str(vid) if vid else None,
        reason=reason,
        pair_correlation_id=pair_correlation_id,
    )
    corr = f"{pair_correlation_id}:{leg}:cancel"
    work = IntentWorkUnit(
        intent=CancelIntent(
            venue_order_id=VenueOrderId(str(vid)) if vid else None,
            client_order_id=ClientOrderId(cid),
            intent_id=IntentId(f"cancel-{cid}"),
        ),
        correlation_id=corr,
    )
    await process_intent_work_unit(
        work,
        app=app,
        run_id=run_id,
        strategy=strategy,
        coord=coord,
        sink=sink,
        oms=oms,
        apply_local_shadow_fill=apply_local_shadow_fill,
        live_clob_client=live_clob_client,
    )
    lo_after = coord.orders.orders.get(ClientOrderId(cid))
    if lo_after is None or lo_after.remaining <= 0:
        pb_facts.emit_entry_cancel_ack(
            sink,
            run_id,
            state,
            yes_book,
            no_book,
            leg=leg,
            client_order_id=cid,
            venue_order_id=str(vid) if vid else None,
            reason=reason,
            pair_correlation_id=pair_correlation_id,
        )
        return True
    pb_facts.emit_entry_cancel_failed(
        sink,
        run_id,
        state,
        yes_book,
        no_book,
        leg=leg,
        client_order_id=cid,
        venue_order_id=str(vid) if vid else None,
        reason=reason,
        pair_correlation_id=pair_correlation_id,
    )
    return False


async def abort_pair_entry(
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
    reason: str,
    blocked_leg: str | None,
    blocking_phase: str,
    reason_codes: tuple[str, ...],
    apply_local_shadow_fill: bool,
    live_clob_client,
    unwind_fn: UnwindFn,
    other_leg_status: str | None = None,
) -> PairEntryAbortOutcome:
    """Cancel resting entry orders and unwind filled inventory with retry."""
    state.pair_entry_saga_phase = PairEntrySagaPhase.PAIR_ABORTING.value
    pair_id = state.pair_correlation_id or "unknown"
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
            reason=reason,
            apply_local_shadow_fill=apply_local_shadow_fill,
            live_clob_client=live_clob_client,
            pair_correlation_id=pair_id,
        )
    yes_qty = _leg_filled_qty(coord, cfg, "yes")
    no_qty = _leg_filled_qty(coord, cfg, "no")
    if blocked_leg:
        pb_facts.emit_entry_leg_blocked(
            sink,
            run_id,
            state,
            yes_book,
            no_book,
            blocked_leg=blocked_leg,
            blocking_phase=blocking_phase,
            reason_codes=list(reason_codes),
            other_leg_status=other_leg_status,
            action_taken="cancel_and_unwind",
        )
        pb_facts.emit_entry_asymmetry_detected(
            sink,
            run_id,
            state,
            yes_book,
            no_book,
            blocked_leg=blocked_leg,
            reason=reason,
            yes_filled_qty=yes_qty,
            no_filled_qty=no_qty,
            action_taken="cancel_and_unwind",
        )
    outcome = EmergencyUnwindOutcome(flat=True, manual_intervention=False, attempt_count=0)
    if yes_qty > 0 or no_qty > 0:
        qty = max(yes_qty, no_qty, state.effective_qty)
        outcome = await unwind_fn(
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            oms=oms,
            strategy=strategy,
            cfg=cfg,
            state=state,
            qty=qty,
            reason=reason,
            apply_local_shadow_fill=apply_local_shadow_fill,
            live_clob_client=live_clob_client,
        )
        state.unwind_attempt_count = outcome.attempt_count
    if outcome.manual_intervention:
        state.pair_entry_saga_phase = PairEntrySagaPhase.PAIR_ABORTED_MANUAL_INTERVENTION.value
        pb_facts.emit_pair_entry_manual_intervention(
            sink, run_id, state, yes_book, no_book, reason=reason, attempt_count=outcome.attempt_count
        )
        transition_phase(state, PairedBinaryPhase.FAILED, reason="manual_intervention_required")
        return PairEntryAbortOutcome(flat=False, manual_intervention=True, reason=reason)
    state.pair_entry_saga_phase = PairEntrySagaPhase.PAIR_ABORTED_FLAT.value
    pb_facts.emit_pair_entry_aborted_flat(sink, run_id, state, yes_book, no_book, reason=reason)
    transition_phase(state, PairedBinaryPhase.FAILED, reason=reason)
    return PairEntryAbortOutcome(flat=True, manual_intervention=False, reason=reason)


def _leg_filled_qty(coord, cfg: PairedBinaryStrategyConfig, leg: str) -> Decimal:
    token_id = TokenId(cfg.yes_token_id if leg == "yes" else cfg.no_token_id)
    snap = resolve_leg_fill_snapshot(coord, token_id=token_id, owner_id=cfg.owner_id)
    ledger = coord.allocation_ledger
    ledger_qty = Decimal("0")
    if ledger is not None:
        ledger_qty = ledger.get_available_allocated(cfg.owner_id, token_id)
    if ledger_qty > 0:
        return ledger_qty
    return snap.sellable_qty


def pair_entry_committed(
    coord,
    cfg: PairedBinaryStrategyConfig,
) -> bool:
    min_eff = resolve_min_effective_pair_qty(
        cfg.min_effective_pair_qty,
        venue_min_size=Decimal("5"),
    )
    yes_q = _leg_filled_qty(coord, cfg, "yes")
    no_q = _leg_filled_qty(coord, cfg, "no")
    return yes_q >= min_eff and no_q >= min_eff


def has_resting_entry_leg(coord, state: PairedBinaryRuntimeState) -> bool:
    for leg_state in (state.yes, state.no):
        cid = leg_state.entry_client_order_id
        if not cid:
            continue
        lo = coord.orders.orders.get(ClientOrderId(cid))
        if lo is not None and lo.side == Side.BUY and lo.remaining > 0:
            st = classify_ack_status(lo.ack_status)
            if st.value in ("RESTING", "SUBMITTED", "PARTIALLY_FILLED"):
                return True
    return False


async def run_pair_entry_from_idle(
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
    yes_book: LegBook,
    no_book: LegBook,
    apply_local_shadow_fill: bool,
    live_clob_client,
    unwind_fn: UnwindFn,
    entry_decision_id: str | None = None,
    entry_decision_ts: float | None = None,
) -> bool:
    """Execute compound pair entry from IDLE. Returns True if saga entered pending/committed."""
    ctx = StrategyContext(coord=coord, market_state=coord.market_state)
    pairs, skip = strategy.evaluate_entry(ctx, pair_correlation_id=pair_correlation_id)
    if skip:
        return False

    pf = preflight_pair_entry(pairs, app=app, coord=coord, run_id=run_id, cfg=cfg)
    pb_facts.emit_pair_preflight(
        sink,
        run_id,
        state,
        yes_book,
        no_book,
        pair_correlation_id=pair_correlation_id,
        yes_pf=pf.yes,
        no_pf=pf.no,
        pair_notional=pf.pair_notional,
        approved=pf.ok,
    )
    if not pf.ok:
        pb_facts.emit_pair_preflight_rejected(
            sink,
            run_id,
            state,
            yes_book,
            no_book,
            pair_correlation_id=pair_correlation_id,
            reason_codes=list(pf.reason_codes),
        )
        return False

    state.pair_entry_saga_phase = PairEntrySagaPhase.PAIR_PREFLIGHTED.value
    old, new, _ = transition_phase(state, PairedBinaryPhase.ENTRY_PLANNED)
    pb_facts.emit_state_change(
        sink, run_id, state, yes_book, no_book, from_phase=old.value, to_phase=new.value
    )

    state.pair_entry_saga_phase = PairEntrySagaPhase.PAIR_SUBMITTING.value
    now = monotonic_s()
    outcomes: list[LegSubmitOutcome] = []

    for intent, ext in pairs:
        ext = dict(ext)
        if entry_decision_id:
            ext["decision_id"] = entry_decision_id
            ext["decision_type"] = "entry_eval"
            ext["pre_decision_snapshot_emitted"] = True
            if entry_decision_ts is not None:
                ext["latency_decision_ts"] = entry_decision_ts
        leg = str(ext.get("leg", ""))
        leg_corr = str(ext.get("leg_correlation_id", pair_correlation_id))
        if leg == "yes":
            state.yes.leg_correlation_id = leg_corr
            state.yes.target_qty = cfg.position_size
        elif leg == "no":
            state.no.leg_correlation_id = leg_corr
            state.no.target_qty = cfg.position_size
        pf_leg = pf.yes if leg == "yes" else pf.no
        work = IntentWorkUnit(intent=intent, correlation_id=leg_corr, intent_fact_extensions=ext)
        await process_intent_work_unit(
            work,
            app=app,
            run_id=run_id,
            strategy=strategy,
            coord=coord,
            sink=sink,
            oms=oms,
            apply_local_shadow_fill=apply_local_shadow_fill,
            live_clob_client=live_clob_client,
        )
        cid = state.yes.entry_client_order_id if leg == "yes" else state.no.entry_client_order_id
        outcome = _inspect_leg_after_submit(
            coord,
            cfg,
            leg=leg,
            client_order_id=cid,
            requested_style=pf_leg.requested_style,
            planned_style=pf_leg.planned_style,
        )
        pb_facts.emit_entry_order_style_applied(
            sink,
            run_id,
            state,
            yes_book,
            no_book,
            leg=leg,
            requested_style=pf_leg.requested_style or "",
            planned_style=pf_leg.planned_style or "",
            venue_style=pf_leg.planned_style or "",
            planner_reason=pf_leg.planner_reason,
            style_mismatch=outcome.style_mismatch,
        )
        outcomes.append(outcome)

        if outcome.risk_denied or outcome.rejected:
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
                reason="entry_leg_blocked",
                blocked_leg=leg,
                blocking_phase="submit",
                reason_codes=outcome.reason_codes or ("risk_denied",),
                apply_local_shadow_fill=apply_local_shadow_fill,
                live_clob_client=live_clob_client,
                unwind_fn=unwind_fn,
                other_leg_status="filled" if _leg_filled_qty(coord, cfg, "yes" if leg == "no" else "no") > 0 else "empty",
            )
            return False

        if outcome.resting and not cfg.allow_resting_entry_orders:
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
                reason="resting_not_allowed",
                blocked_leg=leg,
                blocking_phase="submit",
                reason_codes=("resting_not_allowed",),
                apply_local_shadow_fill=apply_local_shadow_fill,
                live_clob_client=live_clob_client,
                unwind_fn=unwind_fn,
            )
            return False

        if outcome.style_mismatch and not cfg.allow_entry_style_downgrade:
            if app.runtime.execution_mode == ExecutionMode.LIVE:
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
                    reason="entry_style_mismatch",
                    blocked_leg=leg,
                    blocking_phase="submit",
                    reason_codes=("entry_style_mismatch",),
                    apply_local_shadow_fill=apply_local_shadow_fill,
                    live_clob_client=live_clob_client,
                    unwind_fn=unwind_fn,
                )
                return False

    state.pair_entry_saga_phase = PairEntrySagaPhase.PAIR_SUBMITTED.value
    if pair_entry_committed(coord, cfg):
        state.pair_entry_saga_phase = PairEntrySagaPhase.PAIR_COMMITTED.value
        pb_facts.emit_pair_entry_committed(
            sink, run_id, state, yes_book, no_book, pair_correlation_id=pair_correlation_id
        )
        old, new, _ = transition_phase(state, PairedBinaryPhase.BOTH_ENTRY_PENDING)
        pb_facts.emit_state_change(
            sink, run_id, state, yes_book, no_book, from_phase=old.value, to_phase=new.value
        )
        return True

    if has_resting_entry_leg(coord, state):
        state.pair_entry_saga_phase = PairEntrySagaPhase.PAIR_RESTING.value
        state.pair_entry_resting_deadline_ts = now + cfg.pair_entry_resting_timeout_s
    elif _leg_filled_qty(coord, cfg, "yes") > 0 != (_leg_filled_qty(coord, cfg, "no") > 0):
        state.pair_entry_saga_phase = PairEntrySagaPhase.PAIR_PARTIALLY_FILLED.value

    old, new, _ = transition_phase(state, PairedBinaryPhase.BOTH_ENTRY_PENDING)
    state.entry_deadline_ts = now + cfg.pair_entry_fill_timeout_s
    state.pair_entry_submit_deadline_ts = now + cfg.pair_entry_submit_timeout_s
    pb_facts.emit_entry_submitted(
        sink,
        run_id,
        state,
        yes_book,
        no_book,
        pair_cost=(yes_book.ask or Decimal("0")) + (no_book.ask or Decimal("0")),
        yes_spread=(yes_book.ask - yes_book.bid) if yes_book.ask and yes_book.bid else None,
        no_spread=(no_book.ask - no_book.bid) if no_book.ask and no_book.bid else None,
    )
    pb_facts.emit_state_change(
        sink, run_id, state, yes_book, no_book, from_phase=old.value, to_phase=new.value
    )
    return True


async def tick_pair_entry_pending(
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
    live_clob_client,
    unwind_fn: UnwindFn,
) -> str:
    """Monitor BOTH_ENTRY_PENDING for asymmetry/timeouts. Returns action token."""
    now = monotonic_s()
    yes_q = _leg_filled_qty(coord, cfg, "yes")
    no_q = _leg_filled_qty(coord, cfg, "no")

    if pair_entry_committed(coord, cfg) and not has_resting_entry_leg(coord, state):
        state.pair_entry_saga_phase = PairEntrySagaPhase.PAIR_COMMITTED.value
        pb_facts.emit_pair_entry_committed(
            sink,
            run_id,
            state,
            yes_book,
            no_book,
            pair_correlation_id=state.pair_correlation_id or "",
        )
        return "committed"

    if (yes_q > 0) != (no_q > 0):
        if state.pair_entry_resting_deadline_ts and now > state.pair_entry_resting_deadline_ts:
            blocked = "no" if yes_q > no_q else "yes"
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
                reason="asymmetric_entry_timeout",
                blocked_leg=blocked,
                blocking_phase="pending",
                reason_codes=("asymmetric_entry_timeout",),
                apply_local_shadow_fill=apply_local_shadow_fill,
                live_clob_client=live_clob_client,
                unwind_fn=unwind_fn,
            )
            return "aborted"

    if state.entry_deadline_ts and now > state.entry_deadline_ts:
        return "fill_timeout"

    if has_resting_entry_leg(coord, state):
        if state.pair_entry_resting_deadline_ts and now > state.pair_entry_resting_deadline_ts:
            if not cfg.allow_resting_entry_orders:
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
                    reason="resting_timeout",
                    blocked_leg="yes" if yes_q <= 0 else "no",
                    blocking_phase="resting_timeout",
                    reason_codes=("resting_timeout",),
                    apply_local_shadow_fill=apply_local_shadow_fill,
                    live_clob_client=live_clob_client,
                    unwind_fn=unwind_fn,
                )
                return "aborted"

    return "pending"
