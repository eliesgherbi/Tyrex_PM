"""Immutable startup classification. Auto-clear only proven cases."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Mapping

from tyrex_pm.execution.exit_sizing import classify_dust_leftover
from tyrex_pm.execution.session_state import ExecutionSessionState, OperationalDisposition


class AssessmentStatus(str, Enum):
    READY = "READY"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    BLOCKED = "BLOCKED"


class IssueSeverity(str, Enum):
    AUTO = "AUTO"
    ACTION = "ACTION"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, kw_only=True)
class AccountIdentity:
    chain_id: int
    environment: str
    wallet: str
    signer: str | None = None


@dataclass(frozen=True, kw_only=True)
class ReadCoverage:
    complete: bool
    positions_complete: bool
    orders_complete: bool
    activity_complete: bool
    known_tokens_checked: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class Holding:
    condition_id: str
    token_id: str
    shares: Decimal
    redeemable: bool | None = None
    outcome_index: int | None = None
    negative_risk: bool | None = None


@dataclass(frozen=True, kw_only=True)
class OpenOrderView:
    venue_order_id: str
    condition_id: str
    token_id: str
    side: str
    original_size: Decimal | None = None
    size_matched: Decimal | None = None


@dataclass(frozen=True, kw_only=True)
class ActivityView:
    activity_id: str
    condition_id: str
    token_id: str
    shares: Decimal
    cause: str
    venue_trade_id: str | None = None
    side: str | None = None
    observed_at: datetime | None = None
    transaction_hash: str | None = None
    venue_order_id: str | None = None
    activity_type: str | None = None


@dataclass(frozen=True, kw_only=True)
class OrderLookup:
    found: bool
    terminal: bool
    matched_shares: Decimal
    status: str
    not_found: bool = False


@dataclass(frozen=True, kw_only=True)
class MarketLifecycleObservation:
    condition_id: str
    status: str
    payout_per_share: Decimal | None = None
    proof_supported: bool = False
    reason: str = ""
    token_payouts: Mapping[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class PlannedEvidence:
    kind: str
    session_id: str
    fields: Mapping[str, str]


@dataclass(frozen=True, kw_only=True)
class StartupIssue:
    issue_id: str
    severity: IssueSeverity
    code: str
    summary: str
    session_id: str | None = None
    evidence: tuple[PlannedEvidence, ...] = ()


@dataclass(frozen=True, kw_only=True)
class RecoveryProposal:
    issue_id: str
    session_id: str
    condition_id: str
    token_id: str
    action: str
    max_shares: Decimal
    min_price: Decimal
    venue_order_ids: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class StartupAssessment:
    status: AssessmentStatus
    issues: tuple[StartupIssue, ...] = ()
    proposals: tuple[RecoveryProposal, ...] = ()
    wallet_display: str = ""
    summary: str = ""

    @property
    def ready(self) -> bool:
        return self.status is AssessmentStatus.READY


@dataclass(frozen=True, kw_only=True)
class InspectionFacts:
    identity: AccountIdentity | None
    coverage: ReadCoverage
    sessions: tuple[ExecutionSessionState, ...] = ()
    holdings: tuple[Holding, ...] = ()
    open_orders: tuple[OpenOrderView, ...] = ()
    activities: tuple[ActivityView, ...] = ()
    order_lookups: Mapping[str, OrderLookup] = field(default_factory=dict)
    lifecycles: Mapping[str, MarketLifecycleObservation] = field(default_factory=dict)
    bound_wallet: str | None = None
    legacy_unbound: bool = False
    collateral: Decimal | None = None
    closing_holdings: tuple[Holding, ...] | None = None
    closing_open_orders: tuple[OpenOrderView, ...] | None = None
    dust_abs_shares: Decimal = Decimal("0.01")
    dust_max_notional: Decimal = Decimal("0.05")
    sell_size_increment: Decimal = Decimal("0.01")
    min_order_size: Decimal | None = None
    minimum_exit_price: Decimal = Decimal("0.01")
    token_bids: Mapping[str, Decimal] = field(default_factory=dict)
    token_min_order_sizes: Mapping[str, Decimal] = field(default_factory=dict)


def assess(facts: InspectionFacts) -> StartupAssessment:
    issues: list[StartupIssue] = []
    proposals: list[RecoveryProposal] = []
    if facts.identity is None or not facts.coverage.complete:
        reason = facts.coverage.reason or "account visibility is incomplete"
        issues.append(
            StartupIssue(
                issue_id="visibility",
                severity=IssueSeverity.BLOCKED,
                code="ACCOUNT_VISIBILITY_INCOMPLETE",
                summary=reason,
            )
        )
    if facts.identity is not None and facts.bound_wallet and (
        facts.identity.wallet.lower() != facts.bound_wallet.lower()
    ):
        issues.append(
            StartupIssue(
                issue_id="wallet",
                severity=IssueSeverity.BLOCKED,
                code="WALLET_MISMATCH",
                summary="The authenticated wallet does not match the journal binding.",
            )
        )
    if facts.legacy_unbound:
        issues.append(
            StartupIssue(
                issue_id="legacy-wallet",
                severity=IssueSeverity.BLOCKED,
                code="LEGACY_JOURNAL_UNBOUND",
                summary=(
                    "This journal has no wallet binding. Associating one records "
                    "provenance only and does not clear exposure."
                ),
            )
        )
    moved: list[str] = []
    if _holdings_changed(facts.holdings, facts.closing_holdings):
        moved.append("holdings")
    if _orders_changed(facts.open_orders, facts.closing_open_orders):
        moved.append("open orders")
    if moved:
        label = " and ".join(moved)
        issues.append(
            StartupIssue(
                issue_id="inventory-moved",
                severity=IssueSeverity.BLOCKED,
                code="INVENTORY_CHANGED_DURING_ASSESSMENT",
                summary=(
                    f"{label[0].upper()}{label[1:]} changed between the opening and closing reads."
                ),
            )
        )
    owned = _owned_index(facts.sessions)
    issues.extend(_foreign_inventory(facts, owned))
    for session in facts.sessions:
        session_issues, session_proposals = _classify_session(facts, session)
        issues.extend(session_issues)
        proposals.extend(session_proposals)
    status = _status(issues)
    summary = _summary(status, issues)
    wallet = "" if facts.identity is None else _mask(facts.identity.wallet)
    return StartupAssessment(
        status=status,
        issues=tuple(issues),
        proposals=tuple(proposals),
        wallet_display=wallet,
        summary=summary,
    )


def _status(issues: list[StartupIssue]) -> AssessmentStatus:
    if any(issue.severity is IssueSeverity.BLOCKED for issue in issues):
        return AssessmentStatus.BLOCKED
    if any(issue.severity is IssueSeverity.ACTION for issue in issues):
        return AssessmentStatus.ACTION_REQUIRED
    return AssessmentStatus.READY


def _summary(status: AssessmentStatus, issues: list[StartupIssue]) -> str:
    if status is AssessmentStatus.READY:
        if not issues:
            return "Account inspection found nothing that blocks a new entry."
        return "Proven cases were cleared. Nothing further blocks a new entry."
    codes = ", ".join(issue.code for issue in issues if issue.severity is not IssueSeverity.AUTO)
    return f"{status.value}: {codes}"


def _mask(wallet: str) -> str:
    if len(wallet) <= 10:
        return wallet[:2] + "…"
    return wallet[:6] + "…" + wallet[-4:]


def _owned_index(
    sessions: tuple[ExecutionSessionState, ...],
) -> dict[tuple[str, str], list[ExecutionSessionState]]:
    index: dict[tuple[str, str], list[ExecutionSessionState]] = {}
    for session in sessions:
        identity = session.identity
        if identity is None:
            continue
        index.setdefault((identity.market_id, identity.token_id), []).append(session)
    return index


def _holdings_changed(
    opening: tuple[Holding, ...], closing: tuple[Holding, ...] | None
) -> bool:
    if closing is None:
        return False
    return _holding_map(opening) != _holding_map(closing)


def _holding_map(holdings: tuple[Holding, ...]) -> dict[tuple[str, str], Decimal]:
    result: dict[tuple[str, str], Decimal] = {}
    for holding in holdings:
        if holding.shares == 0:
            continue
        result[(holding.condition_id, holding.token_id)] = holding.shares
    return result


def _orders_changed(
    opening: tuple[OpenOrderView, ...],
    closing: tuple[OpenOrderView, ...] | None,
) -> bool:
    if closing is None:
        return False
    return _order_map(opening) != _order_map(closing)


def _order_map(orders: tuple[OpenOrderView, ...]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        sorted(
            (
                order.venue_order_id,
                order.condition_id,
                order.token_id,
                order.side.upper(),
                order.original_size,
                order.size_matched,
            )
            for order in orders
        )
    )


def _proven_payout(
    lifecycle: MarketLifecycleObservation | None, token_id: str
) -> Decimal | None:
    """Payout for this token only. Another token's payout is not reused."""
    if (
        lifecycle is None
        or lifecycle.status != "RESOLVED_FINAL"
        or not lifecycle.proof_supported
    ):
        return None
    if lifecycle.token_payouts:
        return lifecycle.token_payouts.get(token_id)
    return lifecycle.payout_per_share


def _foreign_inventory(
    facts: InspectionFacts,
    owned: dict[tuple[str, str], list[ExecutionSessionState]],
) -> list[StartupIssue]:
    issues: list[StartupIssue] = []
    for holding in facts.holdings:
        if holding.shares <= 0:
            continue
        key = (holding.condition_id, holding.token_id)
        owners = owned.get(key, [])
        if owners:
            continue
        lifecycle = facts.lifecycles.get(holding.condition_id)
        if _proven_payout(lifecycle, holding.token_id) is not None:
            continue
        issues.append(
            StartupIssue(
                issue_id=f"external-holding:{holding.token_id}",
                severity=IssueSeverity.BLOCKED,
                code="EXTERNAL_INVENTORY",
                summary=(
                    f"The wallet holds {holding.shares} shares that no bot session owns. "
                    "They are shown and left untouched."
                ),
            )
        )
    known_orders = {
        order.venue_order_id
        for session in facts.sessions
        for order in session.orders.values()
        if order.venue_order_id
    }
    for order in facts.open_orders:
        if order.venue_order_id in known_orders:
            continue
        issues.append(
            StartupIssue(
                issue_id=f"external-order:{order.venue_order_id}",
                severity=IssueSeverity.BLOCKED,
                code="EXTERNAL_ORDER",
                summary=(
                    f"Open order {order.venue_order_id} is not owned by a bot session. "
                    "It will not be cancelled."
                ),
            )
        )
    return issues


def _is_confirmed_reduction(activity: ActivityView) -> bool:
    side = (activity.side or "").upper()
    cause = (activity.cause or "").lower()
    if side == "BUY" or cause == "buy":
        return False
    if activity.shares <= 0:
        return False
    if side == "SELL" and cause in {"", "sale", "sell", "trade"}:
        return True
    return cause in {"sale", "sell", "merge", "redeem"}


def _confirmed_reductions(
    session: ExecutionSessionState, activities: list[ActivityView]
) -> list[ActivityView]:
    accepted: list[ActivityView] = []
    for activity in activities:
        if not _is_confirmed_reduction(activity):
            continue
        if (
            session.opened_at is not None
            and activity.observed_at is not None
            and activity.observed_at < session.opened_at
        ):
            continue
        if activity.activity_id in session.external_activity_ids:
            continue
        if activity.venue_trade_id and activity.venue_trade_id in session.trades:
            continue
        accepted.append(activity)
    return accepted


def _classify_session(
    facts: InspectionFacts, session: ExecutionSessionState
) -> tuple[list[StartupIssue], list[RecoveryProposal]]:
    identity = session.identity
    session_id = session.session_id or "unknown"
    if identity is None:
        return (
            [
                StartupIssue(
                    issue_id=f"identity:{session_id}",
                    severity=IssueSeverity.BLOCKED,
                    code="MISSING_SESSION_IDENTITY",
                    summary="A retained session has no market identity.",
                    session_id=session.session_id,
                )
            ],
            [],
        )
    lifecycle = facts.lifecycles.get(identity.market_id)
    holding = _holding_for(facts, identity.market_id, identity.token_id)
    balance = Decimal("0") if holding is None else holding.shares
    if session.safe_to_open_entry:
        explained = (
            session.settlement_holding_shares
            if session.operational_disposition is OperationalDisposition.SETTLEMENT_ONLY
            else Decimal("0")
        )
        if balance > explained:
            return (
                [
                    StartupIssue(
                        issue_id=f"reappeared:{session_id}",
                        severity=IssueSeverity.BLOCKED,
                        code="HOLDINGS_REAPPEARED",
                        summary=(
                            "A previously cleared session has a fresh positive holding. "
                            "Clearance is not reused."
                        ),
                        session_id=session.session_id,
                    )
                ],
                [],
            )
        return [], []
    issues: list[StartupIssue] = []
    proposals: list[RecoveryProposal] = []
    owners = [
        item
        for item in facts.sessions
        if item.identity is not None
        and item.identity.market_id == identity.market_id
        and item.identity.token_id == identity.token_id
    ]
    activities = [
        activity
        for activity in facts.activities
        if activity.token_id == identity.token_id and activity.condition_id == identity.market_id
    ]
    if len(owners) > 1 and activities:
        return (
            [
                StartupIssue(
                    issue_id=f"allocation:{identity.token_id}",
                    severity=IssueSeverity.BLOCKED,
                    code="AMBIGUOUS_ALLOCATION",
                    summary=(
                        "More than one session shares this token, so an external "
                        "disposal cannot be allocated."
                    ),
                    session_id=session.session_id,
                )
            ],
            [],
        )
    for order_id, order in session.orders.items():
        for attempt_id in order.attempt_ids:
            if attempt_id in session.resolved_attempt_ids:
                continue
            if order.accepted is not None and not order.ambiguous:
                continue
            lookup_key = order.venue_order_id or ""
            lookup = facts.order_lookups.get(lookup_key)
            if lookup is None or not lookup.found or lookup.not_found:
                issues.append(
                    StartupIssue(
                        issue_id=f"attempt:{attempt_id}",
                        severity=IssueSeverity.BLOCKED,
                        code="UNRESOLVED_ATTEMPT",
                        summary=(
                            "An order submission is still uncertain. A missing open-order "
                            "row, HTTP 404, or fuzzy match is not proof it was rejected."
                        ),
                        session_id=session.session_id,
                    )
                )
                continue
            if lookup.terminal and lookup.matched_shares == 0 and lookup.status.lower() in {
                "cancelled",
                "canceled",
                "expired",
                "rejected",
                "unmatched",
            }:
                issues.append(
                    StartupIssue(
                        issue_id=f"attempt:{attempt_id}",
                        severity=IssueSeverity.AUTO,
                        code="ATTEMPT_RESOLVED_NO_FILL",
                        summary="The venue order is terminal and has no fill.",
                        session_id=session.session_id,
                        evidence=(
                            PlannedEvidence(
                                kind="resolve_attempt",
                                session_id=session_id,
                                fields={
                                    "order_id": order_id,
                                    "attempt_id": attempt_id,
                                    "venue_order_id": lookup_key,
                                    "terminal_status": lookup.status,
                                    "cumulative_matched_shares": "0",
                                    "proof_source": "exact_order",
                                },
                            ),
                        ),
                    )
                )
            else:
                issues.append(
                    StartupIssue(
                        issue_id=f"attempt:{attempt_id}",
                        severity=IssueSeverity.BLOCKED,
                        code="UNRESOLVED_ATTEMPT",
                        summary="The venue order is not a proven no-fill terminal state.",
                        session_id=session.session_id,
                    )
                )
    if any(issue.severity is IssueSeverity.BLOCKED for issue in issues):
        return issues, proposals
    obligation = session.confirmed_position_shares
    reductions = _confirmed_reductions(session, activities)
    reduction_total = sum((item.shares for item in reductions), Decimal("0"))
    if reduction_total > obligation:
        issues.append(
            StartupIssue(
                issue_id=f"activity:{session_id}",
                severity=IssueSeverity.BLOCKED,
                code="QUANTITY_EXCEEDS_OBLIGATION",
                summary="External reductions are larger than the recorded position.",
                session_id=session.session_id,
            )
        )
        return issues, proposals
    expected_remaining = obligation - reduction_total
    if reductions and balance != expected_remaining:
        issues.append(
            StartupIssue(
                issue_id=f"activity:{session_id}",
                severity=IssueSeverity.BLOCKED,
                code="EXTERNAL_DISPOSAL_UNCONFIRMED",
                summary="External reductions do not match the remaining balance.",
                session_id=session.session_id,
            )
        )
        return issues, proposals
    for activity in reductions:
        issues.append(
            StartupIssue(
                issue_id=f"external:{activity.activity_id}",
                severity=IssueSeverity.AUTO,
                code="EXTERNAL_DISPOSAL",
                summary=(
                    f"A {activity.cause} of {activity.shares} shares "
                    "is uniquely allocated to this session."
                ),
                session_id=session.session_id,
                evidence=(
                    PlannedEvidence(
                        kind="external_adjustment",
                        session_id=session_id,
                        fields={
                            "activity_id": activity.activity_id,
                            "token_id": identity.token_id,
                            "condition_id": identity.market_id,
                            "shares": str(activity.shares),
                            "cause": activity.cause,
                            "venue_trade_id": activity.venue_trade_id or "",
                            "transaction_hash": activity.transaction_hash or "",
                            "side": activity.side or "",
                        },
                    ),
                ),
            )
        )
    obligation = expected_remaining
    if balance > obligation:
        issues.append(
            StartupIssue(
                issue_id=f"holding:{session_id}",
                severity=IssueSeverity.BLOCKED,
                code="UNEXPECTED_HOLDING",
                summary=(
                    "The wallet holding is larger than the journal obligation. "
                    "A stored zero is not readiness."
                ),
                session_id=session.session_id,
            )
        )
        return issues, proposals
    payout = _proven_payout(lifecycle, identity.token_id)
    if payout is not None:
        if session.has_unresolved_mutation_attempt or session.entry_match_awaiting_confirmation:
            issues.append(
                StartupIssue(
                    issue_id=f"settlement-uncertain:{session_id}",
                    severity=IssueSeverity.BLOCKED,
                    code="SETTLEMENT_WITH_UNRESOLVED_EXECUTION",
                    summary=(
                        "The market has a supported payout proof, but an order or trade "
                        "is still unresolved."
                    ),
                    session_id=session.session_id,
                )
            )
            return issues, proposals
        issues.append(
            StartupIssue(
                issue_id=f"settlement:{session_id}",
                severity=IssueSeverity.AUTO,
                code="SETTLEMENT_HOLDING",
                summary=(
                    "Finality is proven. Unredeemed shares stay as settlement holdings "
                    "and are not credited as cash."
                ),
                session_id=session.session_id,
                evidence=(
                    PlannedEvidence(
                        kind="settlement",
                        session_id=session_id,
                        fields={
                            "condition_id": identity.market_id,
                            "token_id": identity.token_id,
                            "holding_shares": str(balance),
                            "payout_per_share": str(payout),
                            "proof_source": (
                                "supported_finality"
                                if lifecycle is None
                                else (lifecycle.reason or "supported_finality")
                            ),
                            "supported": "true",
                        },
                    ),
                ),
            )
        )
        return issues, proposals
    if lifecycle is not None and lifecycle.status in {
        "CLOSED_PENDING_RESOLUTION",
        "UNKNOWN",
    }:
        if obligation > 0 or balance > 0 or session.has_journal_uncertainty:
            issues.append(
                StartupIssue(
                    issue_id=f"lifecycle:{session_id}",
                    severity=IssueSeverity.BLOCKED,
                    code="AWAITING_RESOLUTION",
                    summary=(
                        lifecycle.reason
                        or "The market is not tradable and finality is not proven. "
                        "A passed end time or a closed flag is not settlement."
                    ),
                    session_id=session.session_id,
                )
            )
            return issues, proposals
    if session.has_confirmed_buy and balance == 0 and obligation > 0 and not reductions:
        issues.append(
            StartupIssue(
                issue_id=f"lagging-balance:{session_id}",
                severity=IssueSeverity.BLOCKED,
                code="BALANCE_LAGS_ENTRY",
                summary=(
                    "A confirmed buy is still an obligation. A zero balance without "
                    "a proven disposal does not clear it."
                ),
                session_id=session.session_id,
            )
        )
        return issues, proposals
    dust = None
    bid = facts.token_bids.get(identity.token_id)
    min_size = facts.token_min_order_sizes.get(identity.token_id, facts.min_order_size)
    if obligation > 0:
        dust = classify_dust_leftover(
            obligation,
            increment=facts.sell_size_increment,
            min_order_size=min_size,
            last_bid=bid,
            dust_abs_shares=facts.dust_abs_shares,
            dust_max_notional=facts.dust_max_notional,
        )
    flat = (
        obligation <= 0
        and not session.resting_limit_venue_ids
        and not session.has_journal_uncertainty
    )
    if flat:
        proven = any(issue.severity is IssueSeverity.AUTO for issue in issues) or (
            session.external_reduction_shares > 0 or session.dust_residual is not None
        )
        if proven and session.operational_disposition is OperationalDisposition.UNRESOLVED:
            issues.append(_clearance_issue(session_id, "CLEARED_FLAT"))
        return issues, proposals
    if dust is not None and not session.resting_limit_venue_ids:
        issues.append(
            StartupIssue(
                issue_id=f"dust:{session_id}",
                severity=IssueSeverity.AUTO,
                code="CLASSIFIED_DUST",
                summary="The leftover matches the existing dust rule.",
                session_id=session.session_id,
                evidence=(
                    PlannedEvidence(
                        kind="dust",
                        session_id=session_id,
                        fields={
                            "token_id": identity.token_id,
                            "shares": str(obligation),
                            "market_id": identity.market_id,
                            "window_id": identity.window_id,
                            "classification": dust.code,
                            "last_bid": "" if bid is None else str(bid),
                            "min_order_size": "" if min_size is None else str(min_size),
                        },
                    ),
                ),
            )
        )
        return issues, proposals
    if session.resting_limit_venue_ids and obligation <= 0:
        proposals.append(
            RecoveryProposal(
                issue_id=f"cancel:{session_id}",
                session_id=session_id,
                condition_id=identity.market_id,
                token_id=identity.token_id,
                action="cancel",
                max_shares=Decimal("0"),
                min_price=facts.minimum_exit_price,
                venue_order_ids=session.resting_limit_venue_ids,
            )
        )
        issues.append(
            StartupIssue(
                issue_id=f"cancel:{session_id}",
                severity=IssueSeverity.ACTION,
                code="OWNED_LIVE_ORDER",
                summary="A bot order is still live. Cancelling it needs explicit approval.",
                session_id=session.session_id,
            )
        )
        return issues, proposals
    if obligation > 0 and (lifecycle is None or lifecycle.status == "TRADABLE"):
        proposals.append(
            RecoveryProposal(
                issue_id=f"close:{session_id}",
                session_id=session_id,
                condition_id=identity.market_id,
                token_id=identity.token_id,
                action="cancel_and_close",
                max_shares=obligation,
                min_price=facts.minimum_exit_price,
                venue_order_ids=session.resting_limit_venue_ids,
            )
        )
        issues.append(
            StartupIssue(
                issue_id=f"close:{session_id}",
                severity=IssueSeverity.ACTION,
                code="OWNED_POSITION",
                summary=(
                    f"This bot session still has {obligation} tradable shares. "
                    "Closing them needs explicit approval and does not use the new "
                    "strategy's entry budget."
                ),
                session_id=session.session_id,
            )
        )
        return issues, proposals
    if obligation > 0 or session.has_journal_uncertainty or not session.safe_to_open_entry:
        issues.append(
            StartupIssue(
                issue_id=f"unresolved:{session_id}",
                severity=IssueSeverity.BLOCKED,
                code="UNRESOLVED_SESSION",
                summary="This session is not proven flat, settled, or ready for an approved close.",
                session_id=session.session_id,
            )
        )
    return issues, proposals


def _clearance_issue(session_id: str, disposition: str) -> StartupIssue:
    return StartupIssue(
        issue_id=f"clear:{session_id}:{disposition}",
        severity=IssueSeverity.AUTO,
        code="OPERATIONAL_CLEARANCE",
        summary=f"Record {disposition} only from the proof already listed.",
        session_id=session_id,
        evidence=(
            PlannedEvidence(
                kind="clearance",
                session_id=session_id,
                fields={"disposition": disposition, "proof_event_ids": ""},
            ),
        ),
    )


def _holding_for(
    facts: InspectionFacts, condition_id: str, token_id: str
) -> Holding | None:
    for holding in facts.holdings:
        if holding.condition_id == condition_id and holding.token_id == token_id:
            return holding
    return None
