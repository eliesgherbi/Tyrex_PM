"""R7A.2 position acknowledgment validity and readiness impact."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tyrex_pm.runtime.r7_position_ack import (
    ACKNOWLEDGMENT_TEXT,
    AckError,
    ack_targets_forbidden,
    build_acknowledgment,
    fingerprint_from_raw,
    validate_acknowledgment_against_inventory,
)
from tyrex_pm.runtime.r7_position_inventory import build_inventory_report
from tyrex_pm.runtime.r7_readiness import R7Blocker, build_r7_readiness


def _four_raw() -> list[dict]:
    return [
        {
            "conditionId": "0xc1",
            "asset": "t1",
            "outcome": "A",
            "size": "13.5135",
            "redeemable": True,
            "curPrice": 0,
            "slug": "lol-1",
        },
        {
            "conditionId": "0xc2",
            "asset": "t2",
            "outcome": "Up",
            "size": "9.2592",
            "redeemable": True,
            "curPrice": 0,
            "slug": "btc-updown-5m-1",
        },
        {
            "conditionId": "0xc3",
            "asset": "t3",
            "outcome": "Up",
            "size": "5",
            "redeemable": True,
            "curPrice": 0,
            "slug": "btc-updown-5m-2",
        },
        {
            "conditionId": "0xc4",
            "asset": "t4",
            "outcome": "Up",
            "size": "5",
            "redeemable": True,
            "curPrice": 0,
            "slug": "btc-updown-5m-3",
        },
    ]


def test_exact_four_accepted() -> None:
    ack = build_acknowledgment(raw_positions=_four_raw(), commit_identity="abc")
    assert len(ack.positions) == 4
    assert ack.acknowledgment_text_hash
    v = validate_acknowledgment_against_inventory(ack, raw_positions=_four_raw())
    assert v.ok
    assert v.matched == 4


def test_different_quantity_invalidates() -> None:
    ack = build_acknowledgment(raw_positions=_four_raw(), commit_identity="abc")
    changed = _four_raw()
    changed[0]["size"] = "14"
    v = validate_acknowledgment_against_inventory(ack, raw_positions=changed)
    assert not v.ok
    assert "ACKNOWLEDGED_POSITION_SET_CHANGED" in v.blockers or (
        "UNACKNOWLEDGED_POSITION_PRESENT" in v.blockers
    )


def test_new_position_blocks() -> None:
    ack = build_acknowledgment(raw_positions=_four_raw(), commit_identity="abc")
    rows = _four_raw()
    rows.append(
        {
            "conditionId": "0xc5",
            "asset": "t5",
            "outcome": "X",
            "size": "1",
            "redeemable": True,
            "curPrice": 0,
        }
    )
    v = validate_acknowledgment_against_inventory(ack, raw_positions=rows)
    assert not v.ok
    assert "UNACKNOWLEDGED_POSITION_PRESENT" in v.blockers


def test_category_change_blocks() -> None:
    ack = build_acknowledgment(raw_positions=_four_raw(), commit_identity="abc")
    rows = _four_raw()
    rows[1]["redeemable"] = False
    rows[1]["curPrice"] = 0.5
    v = validate_acknowledgment_against_inventory(ack, raw_positions=rows)
    assert not v.ok


def test_selected_token_never_acknowledged_as_flat() -> None:
    ack = build_acknowledgment(raw_positions=_four_raw(), commit_identity="abc")
    v = validate_acknowledgment_against_inventory(
        ack,
        raw_positions=_four_raw(),
        selected_token_ids=["t2"],
        selected_condition_id="0xc2",
    )
    assert not v.ok
    assert "SELECTED_MARKET_POSITION_NONZERO" in v.blockers


def test_ack_removes_only_account_exposure_blocker() -> None:
    inv = build_inventory_report(
        _four_raw(),
        selected_token_ids=["runtime_yes"],
        selected_condition_id="runtime_cond",
        reconciliation_clean=True,
    )
    before = build_r7_readiness(
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
    assert R7Blocker.ACCOUNT_EXPOSURE_PRESENT in before.blockers

    ack = build_acknowledgment(raw_positions=_four_raw(), commit_identity="abc")
    v = validate_acknowledgment_against_inventory(ack, raw_positions=_four_raw())
    after = build_r7_readiness(
        inventory=inv,
        user_stream_ready=True,
        reconciliation_unreachable=False,
        open_order_count=0,
        fee_resolved=True,
        market_window_blocker=None,
        sizing_blocker=None,
        balance_ok=True,
        account_policy="acknowledged_resolved_redeemable",
        acknowledgment_valid=v.ok,
        acknowledgment_blockers=v.blockers,
    )
    assert R7Blocker.ACCOUNT_EXPOSURE_PRESENT not in after.blockers
    assert set(after.blockers) == {
        R7Blocker.MUTATIONS_DISABLED,
        R7Blocker.R7B_AUTHORIZATION_ABSENT,
    }
    assert after.ready_for_r7b_proposal


def test_ack_does_not_hide_fee_blocker() -> None:
    inv = build_inventory_report(
        _four_raw(), selected_token_ids=[], selected_condition_id=None
    )
    ack = build_acknowledgment(raw_positions=_four_raw(), commit_identity="abc")
    v = validate_acknowledgment_against_inventory(ack, raw_positions=_four_raw())
    r = build_r7_readiness(
        inventory=inv,
        user_stream_ready=True,
        reconciliation_unreachable=False,
        open_order_count=0,
        fee_resolved=False,
        market_window_blocker=None,
        sizing_blocker=None,
        balance_ok=True,
        account_policy="acknowledged_resolved_redeemable",
        acknowledgment_valid=v.ok,
    )
    assert R7Blocker.FEE_PARAMETERS_UNKNOWN in r.blockers
    assert not r.ready_for_r7b_proposal


def test_no_sell_redeem_path_targets_ack() -> None:
    ack = build_acknowledgment(raw_positions=_four_raw(), commit_identity="abc")
    for p in ack.positions:
        assert ack_targets_forbidden(p.token_id, ack)
    assert not ack_targets_forbidden("runtime_yes", ack)
    assert ack.prohibitions["sell"] is True
    assert ack.prohibitions["redeem"] is True
    assert ACKNOWLEDGMENT_TEXT in ack.to_dict()["acknowledgment_text"]


def test_wrong_count_raises() -> None:
    with pytest.raises(AckError, match="EXPECTED_FOUR"):
        build_acknowledgment(raw_positions=_four_raw()[:3], commit_identity="abc")
