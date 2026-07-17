"""R7A dry mutation lifecycle using spy transport — zero network mutations."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.execution.polymarket.approval import ApprovalArtifact, validate_approval
from tyrex_pm.execution.polymarket.live_budget import LiveBudgetGuard
from tyrex_pm.execution.polymarket.mutation_lifecycle import MutationLifecycle, MutationPhase
from tyrex_pm.execution.polymarket.mutation_transport import SpyMutationTransport
from tyrex_pm.execution.polymarket.transport import SubmitOrderRequest


@dataclass
class DryLifecycleResult:
    ok: bool
    phase: str
    submits: int
    cancels: int
    budget: dict[str, Any]
    lifecycle: dict[str, Any]
    notes: list[str] = field(default_factory=list)


def run_dry_lifecycle(
    *,
    artifact: ApprovalArtifact,
    budget_path: Path,
    scenario: str = "full_fill_exit",
    commit_identity: str,
    config_fingerprint: str,
) -> DryLifecycleResult:
    """Execute a scripted dry lifecycle. Never touches the network."""
    notes: list[str] = []
    life = MutationLifecycle()
    life.prepare_approval(artifact.artifact_id)
    life.accept_approval()
    # Simulate explicit R7B authorization for dry path only
    life.arm()

    validate_approval(
        artifact,
        commit_identity=commit_identity,
        config_fingerprint=config_fingerprint,
        market_id=artifact.market_id,
        instrument_token_id=artifact.instrument_token_id,
        quantity=Decimal(artifact.quantity),
        limit_price=Decimal(artifact.limit_price),
        max_buy_notional=Decimal(artifact.max_buy_notional),
        user_stream_ready=True,
        reconciliation_clean=True,
    )

    budget = LiveBudgetGuard(path=budget_path)
    if budget_path.exists():
        budget_path.unlink()
    budget.bind_approval(
        approval_artifact_id=artifact.artifact_id,
        market_id=artifact.market_id,
        instrument_id=artifact.instrument_token_id,
        max_buy_notional=Decimal(artifact.max_buy_notional),
    )

    notional = Decimal(artifact.limit_price) * Decimal(artifact.quantity)
    spy = SpyMutationTransport()
    if scenario == "rejected_entry":
        spy.submit_behavior = "reject"
    elif scenario == "unknown_submission":
        spy.submit_behavior = "timeout"

    life.note_entry_submitting()
    budget.reserve_working(notional)
    req = SubmitOrderRequest(
        token_id=artifact.instrument_token_id,
        side="BUY",
        price=artifact.limit_price,
        size=artifact.quantity,
        amount=artifact.max_buy_notional,
        order_type=artifact.order_type,
    )
    result = spy.submit_order(req)

    if scenario == "rejected_entry":
        budget.release_working(notional)
        life.note_entry_rejected()
        notes.append("entry_rejected")
        return DryLifecycleResult(
            ok=life.phase is MutationPhase.MUTATIONS_DISABLED
            or life.phase is MutationPhase.ENTRY_REJECTED,
            phase=life.phase.value,
            submits=len(spy.submitted),
            cancels=len(spy.cancelled),
            budget=budget.state.to_dict(),
            lifecycle=life.to_dict(),
            notes=notes,
        )

    if scenario == "unknown_submission":
        budget.mark_uncertain(notional)
        life.note_entry_unknown()
        notes.append("entry_unknown_reserved")
        return DryLifecycleResult(
            ok=life.phase is MutationPhase.UNKNOWN_SUBMISSION,
            phase=life.phase.value,
            submits=len(spy.submitted),
            cancels=0,
            budget=budget.state.to_dict(),
            lifecycle=life.to_dict(),
            notes=notes,
        )

    assert result.ok and result.venue_order_id
    life.note_entry_accepted()

    if scenario == "partial_fill_cancel":
        fill_n = notional / 2
        budget.apply_fill(fill_n)
        budget.release_working(notional - fill_n)
        life.note_entry_filled(partial=True)  # → POSITION_ACTIVE
        spy.cancel_order(result.venue_order_id)
        # Residual position remains active after canceling unfilled remainder
        notes.append("partial_then_cancel")
    else:
        budget.apply_fill(notional)
        budget.release_working(Decimal("0"))
        life.note_entry_filled(partial=False)
        notes.append("full_fill")

    # Exit
    life.note_exit_submitting()
    exit_req = SubmitOrderRequest(
        token_id=artifact.instrument_token_id,
        side="SELL",
        price=artifact.limit_price,
        size=artifact.quantity if scenario != "partial_fill_cancel" else str(
            Decimal(artifact.quantity) / 2
        ),
        order_type="FAK",
    )
    spy.submit_order(exit_req)
    life.note_exit_filled(partial=False)
    life.note_reconciling()
    life.note_flat_confirmed()
    notes.append("flat_confirmed")

    return DryLifecycleResult(
        ok=life.phase is MutationPhase.MUTATIONS_DISABLED,
        phase=life.phase.value,
        submits=len(spy.submitted),
        cancels=len(spy.cancelled),
        budget=budget.state.to_dict(),
        lifecycle=life.to_dict(),
        notes=notes,
    )
