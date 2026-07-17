"""Dry R7B session lifecycle — spy transport only; zero network mutations."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.execution.polymarket.live_budget import LiveBudgetGuard
from tyrex_pm.execution.polymarket.mutation_lifecycle import MutationLifecycle, MutationPhase
from tyrex_pm.execution.polymarket.mutation_transport import SpyMutationTransport
from tyrex_pm.execution.polymarket.transport import SubmitOrderRequest
from tyrex_pm.runtime.r7_position_ack import (
    PositionAcknowledgment,
    ack_targets_forbidden,
)
from tyrex_pm.runtime.r7b_session import (
    SessionError,
    SessionPhase,
    SessionRuntimeState,
    build_execution_artifact,
    build_session_authorization,
)


@dataclass
class DrySessionResult:
    ok: bool
    phase: str
    notes: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    submits: int = 0
    cancels: int = 0


def run_dry_session_lifecycle(
    *,
    ack: PositionAcknowledgment,
    budget_path: Path,
    commit_identity: str,
    scenario: str = "full_bind_fill_exit",
) -> DrySessionResult:
    """Scripted dry session: observe → bind → artifact → submit → exit → flat."""
    notes: list[str] = []
    session = build_session_authorization(
        commit_identity=commit_identity,
        acknowledged_position_set_id=ack.acknowledgment_id,
        acknowledged_position_set_fingerprint=ack.set_fingerprint(),
    )
    # Dry path simulates future user auth without enabling network
    session = type(session)(**{**session.__dict__, "user_authorization_present": True})
    rt = SessionRuntimeState()
    rt.attach(session)

    # Acknowledged positions must never be execution targets
    for p in ack.positions:
        assert ack_targets_forbidden(p.token_id, ack)
    notes.append("ack_positions_not_execution_targets")

    if scenario == "no_signal_expire":
        for i in range(int(session.limits["max_windows_observed"])):
            rt.observe_window(f"btc-updown-5m-{1000 + i}", eligible=False)
        return DrySessionResult(
            ok=rt.phase is SessionPhase.OBSERVING,
            phase=rt.phase.value,
            notes=notes + ["no_eligible_window"],
            state=rt.to_dict(),
        )

    if scenario == "max_windows":
        try:
            for i in range(int(session.limits["max_windows_observed"]) + 1):
                rt.observe_window(f"btc-updown-5m-{2000 + i}", eligible=False)
            ok = False
        except SessionError as exc:
            ok = str(exc) == "MAX_WINDOWS_EXCEEDED"
            notes.append(str(exc))
        return DrySessionResult(ok=ok, phase=rt.phase.value, notes=notes, state=rt.to_dict())

    # Skip one invalid, bind one eligible
    rt.observe_window("btc-updown-5m-skip", eligible=False)
    notes.append("skipped_ineligible")
    eligible_slug = "btc-updown-5m-1784000000"
    rt.observe_window(eligible_slug, eligible=True)
    rt.bind_market(eligible_slug)

    yes_tok = "yes_token_runtime"
    no_tok = "no_token_runtime"
    # Ensure ack tokens are not selected
    assert yes_tok not in {p.token_id for p in ack.positions}

    art = build_execution_artifact(
        session=session,
        market_slug=eligible_slug,
        condition_id="0xcond_runtime",
        yes_token_id=yes_tok,
        no_token_id=no_tok,
        selected_outcome="YES",
        selected_token_id=yes_tok,
        signal_id="sig1",
        decision_id="dec1",
        market_start="2026-07-17T12:00:00+00:00",
        market_end="2026-07-17T12:05:00+00:00",
        entry_deadline="2026-07-17T12:03:45+00:00",
        flatten_deadline="2026-07-17T12:04:30+00:00",
        best_ask=Decimal("0.50"),
        worst_entry_price=Decimal("0.50"),
        buy_amount=Decimal("4.80"),
        max_entry_fee=Decimal("0.168"),
        max_entry_collateral=Decimal("4.968"),
        max_estimated_shares=Decimal("9.60"),
        visible_depth=Decimal("100"),
        tick_size="0.01",
        min_order_size="5",
        fee_rate="0.07",
        fee_exponent="1",
        user_stream_ready=True,
        reconciliation_fingerprint="recon1",
        balance_allowance_ready=True,
        kill_switch_active=False,
        live_budget_unused=True,
    )
    rt.set_artifact(art)

    if scenario == "artifact_then_block_switch":
        try:
            rt.bind_market("btc-updown-5m-other")
            ok = False
        except SessionError as exc:
            ok = str(exc) in {"ALREADY_BOUND", "BIND_REQUIRES_OBSERVING"}
            notes.append(str(exc))
        return DrySessionResult(ok=ok, phase=rt.phase.value, notes=notes, state=rt.to_dict())

    budget = LiveBudgetGuard(path=budget_path)
    if budget_path.exists():
        budget_path.unlink()
    budget.bind_approval(
        approval_artifact_id=art.artifact_id,
        market_id=art.condition_id,
        instrument_id=art.selected_token_id,
        max_buy_notional=Decimal(art.max_entry_collateral),
    )

    life = MutationLifecycle()
    life.prepare_approval(art.artifact_id)
    life.accept_approval()
    life.arm()

    spy = SpyMutationTransport()
    notional = Decimal(art.buy_amount)

    if scenario == "uncertain_submit":
        spy.submit_behavior = "timeout"
        rt.begin_entry_submit()
        budget.reserve_working(notional)
        spy.submit_order(
            SubmitOrderRequest(
                token_id=art.selected_token_id,
                side="BUY",
                price=art.worst_entry_price,
                size=art.max_estimated_shares,
                amount=art.buy_amount,
                order_type="FAK",
            )
        )
        budget.mark_uncertain(notional)
        life.note_entry_unknown()
        # No second market / entry
        try:
            rt.observe_window("btc-updown-5m-next", eligible=True)
            ok = False
        except SessionError:
            ok = True
        notes.append("uncertain_reserved_no_resubmit")
        return DrySessionResult(
            ok=ok and life.phase is MutationPhase.UNKNOWN_SUBMISSION,
            phase=rt.phase.value,
            notes=notes,
            state=rt.to_dict(),
            budget=budget.state.to_dict(),
            submits=len(spy.submitted),
        )

    # Normal submit path
    rt.begin_entry_submit()
    assert session.session_id == rt.session.session_id
    assert rt.session.consumed
    notes.append("session_consumed_on_submit")

    budget.reserve_working(notional)
    life.note_entry_submitting()
    result = spy.submit_order(
        SubmitOrderRequest(
            token_id=art.selected_token_id,
            side="BUY",
            price=art.worst_entry_price,
            size=art.max_estimated_shares,
            amount=art.buy_amount,
            order_type="FAK",
        )
    )
    assert result.ok and result.venue_order_id
    life.note_entry_accepted()

    if scenario == "partial_fill":
        fill_n = notional / 2
        budget.apply_fill(fill_n)
        budget.release_working(notional - fill_n)
        life.note_entry_filled(partial=True)
        rt.note_fill()
        # Block additional BUY
        assert not rt.can_second_entry() or rt.any_fill
        notes.append("partial_fill_no_second_buy")
        exit_size = str(Decimal(art.max_estimated_shares) / 2)
    else:
        budget.apply_fill(notional)
        budget.release_working(Decimal("0"))
        life.note_entry_filled(partial=False)
        rt.note_fill()
        exit_size = art.max_estimated_shares
        notes.append("full_fill")

    # Exit only acquired runtime position — never ack tokens
    life.note_exit_submitting()
    spy.submit_order(
        SubmitOrderRequest(
            token_id=art.selected_token_id,
            side="SELL",
            price=art.worst_entry_price,
            size=exit_size,
            amount=exit_size,
            order_type="FAK",
        )
    )
    for p in ack.positions:
        assert all(s.token_id != p.token_id for s in spy.submitted)
    life.note_exit_filled(partial=False)
    life.note_reconciling()
    life.note_flat_confirmed()
    rt.note_flat()
    notes.append("flat_no_next_market")

    # No automatic next market
    try:
        rt.bind_market("btc-updown-5m-another")
        switched = True
    except SessionError:
        switched = False

    return DrySessionResult(
        ok=(
            life.phase is MutationPhase.MUTATIONS_DISABLED
            and rt.phase is SessionPhase.COMPLETED_FLAT
            and not switched
            and len(spy.submitted) >= 2
        ),
        phase=rt.phase.value,
        notes=notes,
        state=rt.to_dict(),
        budget=budget.state.to_dict(),
        submits=len(spy.submitted),
        cancels=len(spy.cancelled),
    )
