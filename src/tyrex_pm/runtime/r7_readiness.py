"""Authoritative R7 readiness — distinct from R6D observe-gate readiness."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from tyrex_pm.runtime.r7_position_inventory import InventoryReport


class R7Blocker(str, Enum):
    MUTATIONS_DISABLED = "MUTATIONS_DISABLED"
    # Superseded for operator CLI (r7b-live-once --execute-live). Retained for
    # legacy R7A.2 session-envelope proposal readiness only.
    R7B_AUTHORIZATION_ABSENT = "R7B_AUTHORIZATION_ABSENT"
    USER_STREAM_NOT_READY = "USER_STREAM_NOT_READY"
    RECONCILIATION_NOT_CLEAN = "RECONCILIATION_NOT_CLEAN"
    SELECTED_MARKET_POSITION_NONZERO = "SELECTED_MARKET_POSITION_NONZERO"
    ACCOUNT_EXPOSURE_PRESENT = "ACCOUNT_EXPOSURE_PRESENT"
    UNKNOWN_POSITION_PRESENT = "UNKNOWN_POSITION_PRESENT"
    ACTIVE_UNRELATED_POSITION_PRESENT = "ACTIVE_UNRELATED_POSITION_PRESENT"
    EXISTING_OPEN_ORDER = "EXISTING_OPEN_ORDER"
    FEE_PARAMETERS_UNKNOWN = "FEE_PARAMETERS_UNKNOWN"
    INVALID_MARKET_WINDOW = "INVALID_MARKET_WINDOW"
    MARKET_DURATION_MISMATCH = "MARKET_DURATION_MISMATCH"
    TITLE_TIME_MISMATCH = "TITLE_TIME_MISMATCH"
    ENTRY_DEADLINE_AFTER_FLATTEN = "ENTRY_DEADLINE_AFTER_FLATTEN"
    INSUFFICIENT_TIME_REMAINING = "INSUFFICIENT_TIME_REMAINING"
    MARKET_NOT_ACCEPTING_ORDERS = "MARKET_NOT_ACCEPTING_ORDERS"
    ONE_SIDED_BOOK = "ONE_SIDED_BOOK"
    SIZING_BLOCKED = "SIZING_BLOCKED"
    BALANCE_UNKNOWN = "BALANCE_UNKNOWN"
    ACKNOWLEDGED_POSITION_SET_CHANGED = "ACKNOWLEDGED_POSITION_SET_CHANGED"
    UNACKNOWLEDGED_POSITION_PRESENT = "UNACKNOWLEDGED_POSITION_PRESENT"
    ACKNOWLEDGED_POSITION_BECAME_TRADABLE = "ACKNOWLEDGED_POSITION_BECAME_TRADABLE"
    UNKNOWN_EXTERNAL_ORDER = "UNKNOWN_EXTERNAL_ORDER"
    ACKNOWLEDGMENT_REQUIRED = "ACKNOWLEDGMENT_REQUIRED"


@dataclass
class R7Readiness:
    """Single readiness object consumed by approval + CLI + report."""

    ready_for_r7b_proposal: bool = False
    mutations_enabled: bool = False
    blockers: list[R7Blocker] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def deny(self, blocker: R7Blocker, note: str | None = None) -> None:
        self.ready_for_r7b_proposal = False
        if blocker not in self.blockers:
            self.blockers.append(blocker)
        if note:
            self.notes.append(note)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready_for_r7b_proposal": self.ready_for_r7b_proposal,
            "mutations_enabled": self.mutations_enabled,
            "blockers": [b.value for b in self.blockers],
            "notes": list(self.notes),
            "distinction": {
                "reconciliation_clean": "Local and venue truth agree",
                "position_flat": "The agreed selected-market quantity is zero",
                "mutations_disabled": "Structural R7A default; not the only blocker",
            },
        }


def build_r7_readiness(
    *,
    inventory: InventoryReport,
    user_stream_ready: bool,
    reconciliation_unreachable: bool,
    open_order_count: int,
    fee_resolved: bool,
    market_window_blocker: str | None,
    sizing_blocker: str | None,
    balance_ok: bool,
    account_policy: str = "require_ack_resolved_redeemable",
    mutations_enabled: bool = False,
    acknowledgment_valid: bool | None = None,
    acknowledgment_blockers: Sequence[str] = (),
    authorization_model: str = "legacy_session",
) -> R7Readiness:
    """Build authoritative R7 readiness.

    ``account_policy``:
      - ``require_globally_flat``: any nonzero account exposure blocks
      - ``require_ack_resolved_redeemable``: unresolved redeemable blocks until ack
      - ``acknowledged_resolved_redeemable``: valid ack removes ACCOUNT_EXPOSURE_PRESENT
      - ``dedicated_clean_account``: any exposure blocks (prefer clean wallet)

    ``authorization_model``:
      - ``legacy_session``: R7A.2 session/nonce/verbatim path (superseded for live)
      - ``operator_cli_execute_live``: operator CLI; no R7B_AUTHORIZATION_ABSENT
    """
    r = R7Readiness(mutations_enabled=mutations_enabled)
    r.deny(R7Blocker.MUTATIONS_DISABLED)
    if authorization_model == "legacy_session":
        r.deny(R7Blocker.R7B_AUTHORIZATION_ABSENT)
        r.notes.append(
            "legacy_session_auth_superseded_for_operator_cli_use_execute_live"
        )
    else:
        r.notes.append(
            "operator_cli_auth_model_execute_live_is_sole_mutation_authorization"
        )

    if not user_stream_ready:
        r.deny(R7Blocker.USER_STREAM_NOT_READY)
    if reconciliation_unreachable:
        r.deny(R7Blocker.RECONCILIATION_NOT_CLEAN, "venue account evidence unreachable")
    if open_order_count > 0:
        r.deny(R7Blocker.EXISTING_OPEN_ORDER)
        r.deny(R7Blocker.UNKNOWN_EXTERNAL_ORDER)
    if not inventory.selected_market_flat:
        r.deny(R7Blocker.SELECTED_MARKET_POSITION_NONZERO)
    if inventory.unknown_present:
        r.deny(R7Blocker.UNKNOWN_POSITION_PRESENT)
    if inventory.active_unrelated_present:
        r.deny(R7Blocker.ACTIVE_UNRELATED_POSITION_PRESENT)

    for b in acknowledgment_blockers:
        try:
            r.deny(R7Blocker(b))
        except ValueError:
            r.deny(R7Blocker.ACKNOWLEDGED_POSITION_SET_CHANGED, b)

    if inventory.account_exposure_present:
        if account_policy in {"require_globally_flat", "dedicated_clean_account"}:
            r.deny(
                R7Blocker.ACCOUNT_EXPOSURE_PRESENT,
                f"policy={account_policy}",
            )
        elif account_policy == "acknowledged_resolved_redeemable":
            if acknowledgment_valid is True:
                r.notes.append(
                    "acknowledged_resolved_positions_visible_account_wide_not_blocking"
                )
            elif acknowledgment_valid is False:
                r.deny(R7Blocker.ACKNOWLEDGMENT_REQUIRED, "acknowledgment invalid")
            else:
                r.deny(R7Blocker.ACKNOWLEDGMENT_REQUIRED)
                r.deny(R7Blocker.ACCOUNT_EXPOSURE_PRESENT)
        elif account_policy == "require_ack_resolved_redeemable":
            if inventory.resolved_redeemable_present and not inventory.active_unrelated_present:
                r.deny(
                    R7Blocker.ACCOUNT_EXPOSURE_PRESENT,
                    "resolved redeemable inventoried — requires explicit user ack; "
                    "not CLOB-flattenable",
                )
            else:
                r.deny(R7Blocker.ACCOUNT_EXPOSURE_PRESENT)

    if not fee_resolved:
        r.deny(R7Blocker.FEE_PARAMETERS_UNKNOWN)
    if not balance_ok:
        r.deny(R7Blocker.BALANCE_UNKNOWN)

    if market_window_blocker:
        try:
            r.deny(R7Blocker(market_window_blocker))
        except ValueError:
            r.deny(R7Blocker.INVALID_MARKET_WINDOW, market_window_blocker)

    if sizing_blocker:
        if sizing_blocker == "ONE_SIDED_BOOK":
            r.deny(R7Blocker.ONE_SIDED_BOOK)
        else:
            r.deny(R7Blocker.SIZING_BLOCKED, sizing_blocker)

    # Proposal-ready (legacy): only structural blockers remain.
    # Operator CLI does not use this proposal gate; --execute-live is process-local.
    if authorization_model == "legacy_session":
        structural_only = {
            R7Blocker.MUTATIONS_DISABLED,
            R7Blocker.R7B_AUTHORIZATION_ABSENT,
        }
    else:
        structural_only = {R7Blocker.MUTATIONS_DISABLED}
    r.ready_for_r7b_proposal = set(r.blockers) <= structural_only
    return r


# Explicit reconciliation / account policy recommendation for the report.
RECONCILIATION_POLICY_RECOMMENDATION = {
    "selected_market_mandatory": [
        "No position on either selected YES/NO token",
        "No working order for the selected market",
        "No unresolved fill / unknown submission for the selected market",
        "Local and venue selected-market state agree",
    ],
    "account_wide": [
        "Existing unrelated exposure is inventoried",
        "Total account exposure is known",
        "No unknown external working order exists",
        "Existing exposure does not undermine available balance",
        "New BUY budget remains independently capped at $5",
    ],
    "options": [
        {
            "id": 1,
            "name": "require_globally_flat",
            "summary": "Require globally flat account before R7B",
        },
        {
            "id": 2,
            "name": "require_ack_resolved_redeemable",
            "summary": (
                "Permit known unrelated RESOLVED_REDEEMABLE positions with "
                "explicit acknowledgment; still block active unrelated positions"
            ),
        },
        {
            "id": 3,
            "name": "dedicated_clean_account",
            "summary": "Use a dedicated clean wallet/account for R7B (preferred cleanliness)",
        },
    ],
    "recommendation": {
        "preferred_cleanliness": "dedicated_clean_account",
        "acceptable_with_ack": "require_ack_resolved_redeemable",
        "rationale": (
            "Current four positions are all RESOLVED_REDEEMABLE (curPrice=0), not "
            "CLOB-tradable. Prefer a dedicated clean account for the first live "
            "validation; alternatively acknowledge redeemable inventory and keep "
            "selected-market flat. Do not create/fund a wallet without authorization. "
            "Do not CLOB-sell resolved positions; redemption requires separate auth."
        ),
    },
}


EXIT_POLICY_V1 = {
    "normal_exit_order_type": "FAK",
    "exit_quantity_source": "venue_confirmed_position_only",
    "minimum_acceptable_sell_price": "max(best_bid - 0 ticks initially, tick_size)",
    "price_escalation_steps": [
        "attempt_1: best_bid",
        "attempt_2: best_bid - 1 tick (floor tick_size)",
        "attempt_3: best_bid - 2 ticks",
        "emergency: worst_price = tick_size (still FAK, never uncapped market)",
    ],
    "max_attempts": 3,
    "retry_interval_s": 2.0,
    "market_close_escalation": "at flatten_deadline use emergency worst_price FAK once",
    "empty_or_onesided_bid_book": "MANUAL_INTERVENTION — do not invent liquidity",
    "manual_intervention_when": [
        "no bid after max_attempts",
        "exit unknown submission",
        "venue position nonzero after exit matched claim",
        "flatten_deadline passed with residual",
    ],
    "max_expected_exit_fee_usdc": "computed from fd at exit price × shares",
    "worst_case_loss_statement": (
        "A $5 BUY cap does not guarantee a small realized loss. If the exit book "
        "disappears, theoretical maximum loss of the acquired position may approach "
        "the full acquisition cost (order amount + entry fee), up to the authorized "
        "collateral envelope."
    ),
}
