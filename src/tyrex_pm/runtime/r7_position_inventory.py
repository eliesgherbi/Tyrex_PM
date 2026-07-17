"""Read-only R7 position inventory and classification (no mutations)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence


class PositionCategory(str, Enum):
    SELECTED_TOKEN_ACTIVE_POSITION = "SELECTED_TOKEN_ACTIVE_POSITION"
    OTHER_ACTIVE_TRADABLE_POSITION = "OTHER_ACTIVE_TRADABLE_POSITION"
    RESOLVED_REDEEMABLE_POSITION = "RESOLVED_REDEEMABLE_POSITION"
    CLOSED_NONTRADABLE_POSITION = "CLOSED_NONTRADABLE_POSITION"
    DUST_POSITION = "DUST_POSITION"
    UNKNOWN_POSITION = "UNKNOWN_POSITION"


@dataclass(frozen=True)
class CategoryPolicy:
    category: PositionCategory
    blocks_r7b_absolutely: bool
    blocks_account_global_risk: bool
    affects_selected_token_lifecycle: bool
    requires_manual_action: bool
    requires_separate_sell_authorization: bool
    requires_separate_redemption_authorization: bool
    currently_actionable_via_clob: bool
    notes: str


CATEGORY_POLICIES: dict[PositionCategory, CategoryPolicy] = {
    PositionCategory.SELECTED_TOKEN_ACTIVE_POSITION: CategoryPolicy(
        category=PositionCategory.SELECTED_TOKEN_ACTIVE_POSITION,
        blocks_r7b_absolutely=True,
        blocks_account_global_risk=True,
        affects_selected_token_lifecycle=True,
        requires_manual_action=True,
        requires_separate_sell_authorization=True,
        requires_separate_redemption_authorization=False,
        currently_actionable_via_clob=True,
        notes="Selected YES/NO must be flat before R7B entry.",
    ),
    PositionCategory.OTHER_ACTIVE_TRADABLE_POSITION: CategoryPolicy(
        category=PositionCategory.OTHER_ACTIVE_TRADABLE_POSITION,
        blocks_r7b_absolutely=False,
        blocks_account_global_risk=True,
        affects_selected_token_lifecycle=False,
        requires_manual_action=True,
        requires_separate_sell_authorization=True,
        requires_separate_redemption_authorization=False,
        currently_actionable_via_clob=True,
        notes="Known unrelated active exposure; policy choice required.",
    ),
    PositionCategory.RESOLVED_REDEEMABLE_POSITION: CategoryPolicy(
        category=PositionCategory.RESOLVED_REDEEMABLE_POSITION,
        blocks_r7b_absolutely=False,
        blocks_account_global_risk=True,
        affects_selected_token_lifecycle=False,
        requires_manual_action=True,
        requires_separate_sell_authorization=False,
        requires_separate_redemption_authorization=True,
        currently_actionable_via_clob=False,
        notes="Not CLOB-sellable; requires separate on-chain redemption auth.",
    ),
    PositionCategory.CLOSED_NONTRADABLE_POSITION: CategoryPolicy(
        category=PositionCategory.CLOSED_NONTRADABLE_POSITION,
        blocks_r7b_absolutely=False,
        blocks_account_global_risk=True,
        affects_selected_token_lifecycle=False,
        requires_manual_action=True,
        requires_separate_sell_authorization=False,
        requires_separate_redemption_authorization=False,
        currently_actionable_via_clob=False,
        notes="Closed/nontradable; acknowledge only.",
    ),
    PositionCategory.DUST_POSITION: CategoryPolicy(
        category=PositionCategory.DUST_POSITION,
        blocks_r7b_absolutely=False,
        blocks_account_global_risk=True,
        affects_selected_token_lifecycle=False,
        requires_manual_action=True,
        requires_separate_sell_authorization=False,
        requires_separate_redemption_authorization=False,
        currently_actionable_via_clob=False,
        notes="Dust threshold explicit; still inventoried.",
    ),
    PositionCategory.UNKNOWN_POSITION: CategoryPolicy(
        category=PositionCategory.UNKNOWN_POSITION,
        blocks_r7b_absolutely=True,
        blocks_account_global_risk=True,
        affects_selected_token_lifecycle=True,
        requires_manual_action=True,
        requires_separate_sell_authorization=False,
        requires_separate_redemption_authorization=False,
        currently_actionable_via_clob=False,
        notes="Unknown positions block R7B.",
    ),
}


DUST_ABS_SIZE = Decimal("0.01")


@dataclass
class InventoryRow:
    category: PositionCategory
    market_title: str | None
    condition_id_prefix: str | None
    slug: str | None
    outcome: str | None
    quantity: str
    market_status: str
    market_end: str | None
    redeemable: bool | None
    cur_price: str | None
    matches_selected_token: bool
    matches_selected_market: bool
    likely_historical_tyrex: bool | None
    is_dust: bool
    local_persistence_known: bool
    associated_open_order: bool
    affects_5usd_budget: bool
    policy: CategoryPolicy
    cash_pnl: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "market_title": self.market_title,
            "condition_id_prefix": self.condition_id_prefix,
            "slug": self.slug,
            "outcome": self.outcome,
            "quantity": self.quantity,
            "market_status": self.market_status,
            "market_end": self.market_end,
            "redeemable": self.redeemable,
            "cur_price": self.cur_price,
            "matches_selected_token": self.matches_selected_token,
            "matches_selected_market": self.matches_selected_market,
            "likely_historical_tyrex": self.likely_historical_tyrex,
            "is_dust": self.is_dust,
            "local_persistence_known": self.local_persistence_known,
            "associated_open_order": self.associated_open_order,
            "affects_5usd_budget": self.affects_5usd_budget,
            "cash_pnl": self.cash_pnl,
            "policy": {
                "blocks_r7b_absolutely": self.policy.blocks_r7b_absolutely,
                "blocks_account_global_risk": self.policy.blocks_account_global_risk,
                "affects_selected_token_lifecycle": self.policy.affects_selected_token_lifecycle,
                "requires_manual_action": self.policy.requires_manual_action,
                "requires_separate_sell_authorization": (
                    self.policy.requires_separate_sell_authorization
                ),
                "requires_separate_redemption_authorization": (
                    self.policy.requires_separate_redemption_authorization
                ),
                "currently_actionable_via_clob": self.policy.currently_actionable_via_clob,
                "notes": self.policy.notes,
            },
        }


@dataclass
class InventoryReport:
    rows: list[InventoryRow] = field(default_factory=list)
    selected_market_flat: bool = True
    selected_token_flat: bool = True
    account_exposure_present: bool = False
    unknown_present: bool = False
    active_unrelated_present: bool = False
    resolved_redeemable_present: bool = False
    reconciliation_clean: bool = False
    position_flat_selected: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "positions": [r.to_dict() for r in self.rows],
            "selected_market_flat": self.selected_market_flat,
            "selected_token_flat": self.selected_token_flat,
            "account_exposure_present": self.account_exposure_present,
            "unknown_present": self.unknown_present,
            "active_unrelated_present": self.active_unrelated_present,
            "resolved_redeemable_present": self.resolved_redeemable_present,
            "reconciliation_clean": self.reconciliation_clean,
            "position_flat_selected": self.position_flat_selected,
            "distinction": {
                "reconciliation_clean": "Local and venue truth agree",
                "position_flat": "The agreed quantity is zero",
            },
        }


def _prefix(value: Any, n: int = 18) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s[:n] + "…" if len(s) > n else s


def classify_raw_position(
    row: Mapping[str, Any],
    *,
    selected_token_ids: Sequence[str],
    selected_condition_id: str | None,
    open_order_token_ids: Sequence[str] = (),
    local_known_token_ids: Sequence[str] = (),
) -> InventoryRow:
    size = Decimal(str(row.get("size") or "0"))
    asset = str(row.get("asset") or row.get("token_id") or "")
    condition = str(row.get("conditionId") or row.get("condition_id") or "")
    redeemable = row.get("redeemable")
    if redeemable is not None:
        redeemable = bool(redeemable)
    cur = row.get("curPrice")
    cur_d = None if cur is None else Decimal(str(cur))
    slug = row.get("slug") or row.get("eventSlug")
    title = row.get("title")
    is_dust = abs(size) > 0 and abs(size) < DUST_ABS_SIZE
    matches_token = asset in set(selected_token_ids) if asset else False
    matches_market = bool(
        selected_condition_id and condition and condition == selected_condition_id
    )

    # Classification priority
    if size == 0:
        cat = PositionCategory.CLOSED_NONTRADABLE_POSITION
        status = "zero"
    elif matches_token and (cur_d is None or cur_d > 0) and not redeemable:
        cat = PositionCategory.SELECTED_TOKEN_ACTIVE_POSITION
        status = "active"
    elif matches_market and not matches_token and (cur_d is None or cur_d > 0):
        # Opposite token in same market
        cat = PositionCategory.SELECTED_TOKEN_ACTIVE_POSITION
        status = "active_opposite_token"
    elif redeemable is True or (cur_d is not None and cur_d == 0):
        cat = PositionCategory.RESOLVED_REDEEMABLE_POSITION
        status = "resolved_redeemable"
    elif is_dust:
        cat = PositionCategory.DUST_POSITION
        status = "dust"
    elif cur_d is not None and cur_d > 0 and redeemable is False:
        cat = PositionCategory.OTHER_ACTIVE_TRADABLE_POSITION
        status = "active_unrelated"
    elif row.get("closed") is True:
        cat = PositionCategory.CLOSED_NONTRADABLE_POSITION
        status = "closed"
    else:
        cat = PositionCategory.UNKNOWN_POSITION
        status = "unknown"

    # Historical Tyrex: ~$5 BTC up/down losers are consistent with prior tiny tests
    likely_tyrex = None
    if slug and str(slug).startswith("btc-updown-5m-"):
        likely_tyrex = True
    elif title and "LoL:" in str(title):
        likely_tyrex = False

    policy = CATEGORY_POLICIES[cat]
    return InventoryRow(
        category=cat,
        market_title=str(title) if title else None,
        condition_id_prefix=_prefix(condition),
        slug=str(slug) if slug else None,
        outcome=str(row.get("outcome")) if row.get("outcome") is not None else None,
        quantity=str(size),
        market_status=status,
        market_end=str(row.get("endDate")) if row.get("endDate") is not None else None,
        redeemable=redeemable,
        cur_price=None if cur_d is None else str(cur_d),
        matches_selected_token=matches_token,
        matches_selected_market=matches_market,
        likely_historical_tyrex=likely_tyrex,
        is_dust=is_dust,
        local_persistence_known=asset in set(local_known_token_ids),
        associated_open_order=asset in set(open_order_token_ids),
        affects_5usd_budget=False,  # resolved/unrelated do not consume new $5 BUY budget
        policy=policy,
        cash_pnl=str(row.get("cashPnl")) if row.get("cashPnl") is not None else None,
    )


def build_inventory_report(
    raw_positions: Sequence[Mapping[str, Any]],
    *,
    selected_token_ids: Sequence[str],
    selected_condition_id: str | None,
    open_order_token_ids: Sequence[str] = (),
    local_known_token_ids: Sequence[str] = (),
    reconciliation_clean: bool = False,
) -> InventoryReport:
    rows = [
        classify_raw_position(
            r,
            selected_token_ids=selected_token_ids,
            selected_condition_id=selected_condition_id,
            open_order_token_ids=open_order_token_ids,
            local_known_token_ids=local_known_token_ids,
        )
        for r in raw_positions
        if Decimal(str(r.get("size") or "0")) != 0
    ]
    selected_nonzero = any(
        r.matches_selected_token or r.matches_selected_market for r in rows
    )
    return InventoryReport(
        rows=rows,
        selected_market_flat=not selected_nonzero,
        selected_token_flat=not any(r.matches_selected_token for r in rows),
        account_exposure_present=len(rows) > 0,
        unknown_present=any(r.category is PositionCategory.UNKNOWN_POSITION for r in rows),
        active_unrelated_present=any(
            r.category is PositionCategory.OTHER_ACTIVE_TRADABLE_POSITION for r in rows
        ),
        resolved_redeemable_present=any(
            r.category is PositionCategory.RESOLVED_REDEEMABLE_POSITION for r in rows
        ),
        reconciliation_clean=reconciliation_clean,
        position_flat_selected=not selected_nonzero,
    )
