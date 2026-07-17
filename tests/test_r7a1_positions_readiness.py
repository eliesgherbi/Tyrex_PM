"""R7A.1 position classification and unified readiness."""

from __future__ import annotations

from tyrex_pm.runtime.r7_position_inventory import (
    PositionCategory,
    build_inventory_report,
    classify_raw_position,
)
from tyrex_pm.runtime.r7_readiness import R7Blocker, build_r7_readiness


def _resolved_btc(asset: str = "tokA") -> dict:
    return {
        "title": "Bitcoin Up or Down - July 2, 3:50AM-3:55AM ET",
        "slug": "btc-updown-5m-1782978600",
        "outcome": "Up",
        "size": 5,
        "avgPrice": 0.51,
        "curPrice": 0,
        "redeemable": True,
        "endDate": "2026-07-02",
        "conditionId": "condA",
        "asset": asset,
        "cashPnl": -2.55,
    }


def test_resolved_redeemable_classified() -> None:
    row = classify_raw_position(
        _resolved_btc(),
        selected_token_ids=["selected"],
        selected_condition_id="other",
    )
    assert row.category is PositionCategory.RESOLVED_REDEEMABLE_POSITION
    assert row.policy.requires_separate_redemption_authorization
    assert not row.policy.currently_actionable_via_clob
    assert not row.policy.blocks_r7b_absolutely


def test_selected_token_active_blocks() -> None:
    row = classify_raw_position(
        {
            "title": "m",
            "size": 10,
            "curPrice": 0.5,
            "redeemable": False,
            "asset": "sel",
            "conditionId": "c1",
        },
        selected_token_ids=["sel"],
        selected_condition_id="c1",
    )
    assert row.category is PositionCategory.SELECTED_TOKEN_ACTIVE_POSITION
    assert row.policy.blocks_r7b_absolutely


def test_opposite_token_same_market_blocks() -> None:
    row = classify_raw_position(
        {
            "title": "m",
            "size": 10,
            "curPrice": 0.4,
            "redeemable": False,
            "asset": "no_tok",
            "conditionId": "c1",
        },
        selected_token_ids=["yes_tok"],
        selected_condition_id="c1",
    )
    assert row.category is PositionCategory.SELECTED_TOKEN_ACTIVE_POSITION


def test_clean_recon_nonzero_is_clean_but_not_flat() -> None:
    inv = build_inventory_report(
        [_resolved_btc()],
        selected_token_ids=["x"],
        selected_condition_id="y",
        reconciliation_clean=True,
    )
    assert inv.reconciliation_clean is True
    assert inv.account_exposure_present is True
    assert inv.selected_market_flat is True
    assert inv.position_flat_selected is True


def test_unknown_blocks() -> None:
    row = classify_raw_position(
        {"size": 1, "asset": "z", "conditionId": "c"},
        selected_token_ids=[],
        selected_condition_id=None,
    )
    assert row.category is PositionCategory.UNKNOWN_POSITION


def test_readiness_exposes_all_blockers_not_just_mutations() -> None:
    inv = build_inventory_report(
        [_resolved_btc()],
        selected_token_ids=["x"],
        selected_condition_id="y",
        reconciliation_clean=True,
    )
    r = build_r7_readiness(
        inventory=inv,
        user_stream_ready=True,
        reconciliation_unreachable=False,
        open_order_count=0,
        fee_resolved=True,
        market_window_blocker=None,
        sizing_blocker=None,
        balance_ok=True,
        account_policy="require_ack_resolved_redeemable",
    )
    assert R7Blocker.MUTATIONS_DISABLED in r.blockers
    assert R7Blocker.ACCOUNT_EXPOSURE_PRESENT in r.blockers
    assert R7Blocker.R7B_AUTHORIZATION_ABSENT in r.blockers
    assert not r.ready_for_r7b_proposal
    assert r.mutations_enabled is False


def test_selected_position_blocker_in_readiness() -> None:
    inv = build_inventory_report(
        [
            {
                "size": 3,
                "curPrice": 0.5,
                "redeemable": False,
                "asset": "sel",
                "conditionId": "c1",
                "title": "active",
            }
        ],
        selected_token_ids=["sel"],
        selected_condition_id="c1",
    )
    r = build_r7_readiness(
        inventory=inv,
        user_stream_ready=True,
        reconciliation_unreachable=False,
        open_order_count=0,
        fee_resolved=True,
        market_window_blocker=None,
        sizing_blocker=None,
        balance_ok=True,
    )
    assert R7Blocker.SELECTED_MARKET_POSITION_NONZERO in r.blockers
