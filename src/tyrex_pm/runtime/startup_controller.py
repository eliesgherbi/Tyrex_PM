"""Startup inspection and approved recovery. Not a second trading engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Awaitable, Callable
from uuid import uuid4

from tyrex_pm.execution.evidence import (
    DustResidualRecorded,
    ExternalPositionAdjustmentObserved,
    MarketSettlementObserved,
    OrderAttemptResolved,
    RecoveryActionAuthorized,
    RecoveryActionFinished,
    SessionOperationalClearanceRecorded,
    SessionOperationalClearanceRevoked,
    new_envelope,
)
from tyrex_pm.execution.reducer import ReducerInvariantError, assert_clearance_appendable
from tyrex_pm.execution.startup_reconciliation import (
    InspectionFacts,
    IssueSeverity,
    PlannedEvidence,
    RecoveryProposal,
    StartupAssessment,
    assess,
)


class StartupController:
    def __init__(
        self,
        *,
        coordinator,
        now: Callable[[], datetime] | None = None,
        generation: str = "",
        authorization_ttl_s: float = 120,
    ) -> None:
        self.coordinator = coordinator
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.generation = generation
        self.authorization_ttl_s = authorization_ttl_s
        self.mutations: list[str] = []

    async def inspect(self, facts: InspectionFacts) -> StartupAssessment:
        """Persist read-derived evidence, then classify again. Never submits."""
        assessment = assess(facts)
        await self._persist_auto(assessment)
        refreshed = self._refresh_sessions(facts)
        return assess(refreshed)

    async def recheck(self, facts: InspectionFacts) -> StartupAssessment:
        return await self.inspect(facts)

    def build_proposal(self, assessment: StartupAssessment, issue_id: str) -> RecoveryProposal:
        for proposal in assessment.proposals:
            if proposal.issue_id == issue_id:
                return proposal
        raise KeyError(issue_id)

    async def authorize(
        self,
        proposal: RecoveryProposal,
        *,
        approved: bool,
        live: bool,
    ) -> bool:
        if not approved or not live:
            return False
        expires = self.now() + timedelta(seconds=self.authorization_ttl_s)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        await self.coordinator.apply(
            RecoveryActionAuthorized(
                **new_envelope(
                    session_id=proposal.session_id,
                    dedupe_key=f"recovery-authorized:{proposal.issue_id}:{self.generation}",
                ),
                action_id=proposal.issue_id,
                token_id=proposal.token_id,
                condition_id=proposal.condition_id,
                max_shares=proposal.max_shares,
                min_price=proposal.min_price,
                expires_at=expires,
                generation=self.generation,
                action=proposal.action,
                venue_order_ids=tuple(proposal.venue_order_ids),
            )
        )
        self.coordinator.recovery_generation = self.generation
        return True

    def refresh_still_valid(
        self,
        proposal: RecoveryProposal,
        *,
        shares: Decimal,
        condition_id: str,
        token_id: str,
        lifecycle_status: str,
    ) -> str | None:
        if condition_id != proposal.condition_id or token_id != proposal.token_id:
            return "recovery_identity_changed"
        if shares > proposal.max_shares:
            return "recovery_quantity_above_ceiling"
        if lifecycle_status not in {"TRADABLE", ""}:
            return "recovery_market_not_tradable"
        return None

    async def finish(self, proposal: RecoveryProposal, outcome: str) -> None:
        await self.coordinator.apply(
            RecoveryActionFinished(
                **new_envelope(
                    session_id=proposal.session_id,
                    dedupe_key=f"recovery-finished:{proposal.issue_id}:{outcome}",
                ),
                action_id=proposal.issue_id,
                outcome=outcome,
            )
        )

    async def _persist_auto(self, assessment: StartupAssessment) -> None:
        if _prerequisite_blocked(assessment):
            return
        applied_by_session: dict[str, list[str]] = {}
        for issue in assessment.issues:
            if issue.severity is not IssueSeverity.AUTO or issue.code in _PREREQUISITE_CODES:
                continue
            for planned in issue.evidence:
                if planned.kind == "clearance":
                    continue
                event = _planned_event(planned)
                if event is None:
                    continue
                await self.coordinator.apply(event)
                applied_by_session.setdefault(planned.session_id, []).append(event.event_id)
        for issue in assessment.issues:
            if issue.severity is not IssueSeverity.AUTO:
                continue
            for planned in issue.evidence:
                if planned.kind != "clearance":
                    continue
                if planned.fields.get("disposition") == "SETTLEMENT_ONLY":
                    continue
                proof = tuple(applied_by_session.get(planned.session_id, ()))
                state = self.coordinator.states.get(planned.session_id)
                if not proof:
                    proof = _recorded_proof_ids(self.coordinator.journal, planned.session_id)
                if not proof or state is None:
                    continue
                if any(event_id not in state.applied_event_ids for event_id in proof):
                    continue
                clearance = SessionOperationalClearanceRecorded(
                    **new_envelope(
                        session_id=planned.session_id,
                        dedupe_key=(
                            f"operational-clearance:{planned.session_id}:"
                            f"{planned.fields.get('disposition')}:{','.join(proof)}"
                        ),
                    ),
                    disposition=str(planned.fields.get("disposition")),
                    proof_event_ids=proof,
                )
                try:
                    assert_clearance_appendable(state, clearance)
                except ReducerInvariantError:
                    continue
                await self.coordinator.apply(clearance)
        await self._revoke_reappeared(assessment)

    def _refresh_sessions(self, facts: InspectionFacts) -> InspectionFacts:
        sessions = []
        for session in facts.sessions:
            current = self.coordinator.states.get(session.session_id or "")
            sessions.append(session if current is None else current)
        return InspectionFacts(
            identity=facts.identity,
            coverage=facts.coverage,
            sessions=tuple(sessions),
            holdings=facts.holdings,
            open_orders=facts.open_orders,
            activities=facts.activities,
            order_lookups=facts.order_lookups,
            lifecycles=facts.lifecycles,
            bound_wallet=facts.bound_wallet,
            legacy_unbound=facts.legacy_unbound,
            collateral=facts.collateral,
            closing_holdings=facts.closing_holdings,
            closing_open_orders=facts.closing_open_orders,
            dust_abs_shares=facts.dust_abs_shares,
            dust_max_notional=facts.dust_max_notional,
            sell_size_increment=facts.sell_size_increment,
            min_order_size=facts.min_order_size,
            minimum_exit_price=facts.minimum_exit_price,
            token_bids=facts.token_bids,
            token_min_order_sizes=facts.token_min_order_sizes,
        )

    async def _revoke_reappeared(self, assessment: StartupAssessment) -> None:
        if _prerequisite_blocked(assessment):
            return
        for issue in assessment.issues:
            if issue.code != "HOLDINGS_REAPPEARED" or not issue.session_id:
                continue
            state = self.coordinator.states.get(issue.session_id)
            if state is None or state.operational_disposition.value == "UNRESOLVED":
                continue
            await self.coordinator.apply(
                SessionOperationalClearanceRevoked(
                    **new_envelope(
                        session_id=issue.session_id,
                        dedupe_key=f"clearance-revoked:holdings-reappeared:{issue.session_id}",
                    ),
                    reason="HOLDINGS_REAPPEARED",
                )
            )


_PREREQUISITE_CODES = frozenset(
    {
        "ACCOUNT_VISIBILITY_INCOMPLETE",
        "WALLET_MISMATCH",
        "LEGACY_JOURNAL_UNBOUND",
        "INVENTORY_CHANGED_DURING_ASSESSMENT",
    }
)


def _prerequisite_blocked(assessment: StartupAssessment) -> bool:
    return any(issue.code in _PREREQUISITE_CODES for issue in assessment.issues)


def _recorded_proof_ids(journal: object, session_id: str) -> tuple[str, ...]:
    """Proof ids already reduced for this session. Never an unrelated event."""
    load = getattr(journal, "load", None)
    if not callable(load):
        return ()
    names = {
        "ExternalPositionAdjustmentObserved",
        "DustResidualRecorded",
        "OrderAttemptResolved",
        "MarketSettlementObserved",
    }
    return tuple(
        event.event_id
        for event in load(session_id)
        if type(event).__name__ in names
    )


def _planned_event(planned: PlannedEvidence):
    fields = planned.fields
    envelope = new_envelope(
        session_id=planned.session_id,
        dedupe_key=f"startup:{planned.kind}:{fields.get('activity_id', '')}:"
        f"{fields.get('attempt_id', '')}:{fields.get('proof_source', '')}:"
        f"{uuid4()}",
    )
    # Stable dedupe so a repeated observation does not append a second adjustment.
    if planned.kind == "external_adjustment":
        envelope = new_envelope(
            session_id=planned.session_id,
            dedupe_key=f"external-adjustment:{fields['activity_id']}",
        )
        return ExternalPositionAdjustmentObserved(
            **envelope,
            activity_id=fields["activity_id"],
            token_id=fields["token_id"],
            condition_id=fields["condition_id"],
            shares=Decimal(fields["shares"]),
            cause=fields["cause"],
            venue_trade_id=fields.get("venue_trade_id") or None,
        )
    if planned.kind == "resolve_attempt":
        envelope = new_envelope(
            session_id=planned.session_id,
            dedupe_key=f"attempt-resolved:{fields['attempt_id']}",
        )
        return OrderAttemptResolved(
            **envelope,
            order_id=fields["order_id"],
            attempt_id=fields["attempt_id"],
            venue_order_id=fields["venue_order_id"],
            terminal_status=fields["terminal_status"],
            cumulative_matched_shares=Decimal(fields["cumulative_matched_shares"]),
            proof_source=fields["proof_source"],
        )
    if planned.kind == "dust":
        envelope = new_envelope(
            session_id=planned.session_id,
            dedupe_key=f"startup-dust:{fields['token_id']}:{fields['shares']}",
        )
        return DustResidualRecorded(
            **envelope,
            token_id=fields["token_id"],
            shares=Decimal(fields["shares"]),
            market_id=fields["market_id"],
            window_id=fields["window_id"],
            classification=fields["classification"],
        )
    if planned.kind == "settlement":
        envelope = new_envelope(
            session_id=planned.session_id,
            dedupe_key=(
                f"settlement:{fields['condition_id']}:{fields['token_id']}:"
                f"{fields['proof_source']}"
            ),
        )
        return MarketSettlementObserved(
            **envelope,
            condition_id=fields["condition_id"],
            token_id=fields["token_id"],
            holding_shares=Decimal(fields["holding_shares"]),
            payout_per_share=Decimal(fields["payout_per_share"]),
            proof_source=fields["proof_source"],
            supported=fields.get("supported") == "true",
        )
    return None


async def run_approved_exit(
    *,
    lifecycle,
    proposal: RecoveryProposal,
    submit: Callable[..., Awaitable[object]],
) -> object:
    """Resume the owned session and sell through the existing lifecycle."""
    lifecycle.resume(proposal.session_id)
    return await submit(proposal)
