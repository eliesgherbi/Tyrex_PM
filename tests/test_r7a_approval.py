"""R7A approval artifact validation tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.execution.polymarket.approval import (
    ApprovalError,
    build_approval_artifact,
    mark_consumed,
    read_approval,
    validate_approval,
    write_approval,
)

def _art(**over):
    now = datetime.now(timezone.utc)
    kwargs = dict(
        commit_identity="abc123",
        config_fingerprint="cfg1",
        r7a_report_hash="rep1",
        market_id="m1",
        condition_id="c1",
        instrument_token_id="tok1",
        outcome_side="YES",
        max_buy_notional=Decimal("5.00"),
        limit_price=Decimal("0.50"),
        quantity=Decimal("10"),
        order_type="FAK",
        tick_size="0.01",
        min_order_size="5",
        max_limit_price=Decimal("0.50"),
        entry_deadline=now + timedelta(minutes=10),
        flatten_deadline=now + timedelta(minutes=8),
        max_hold_s=180.0,
        flatten_before_close_s=30.0,
        risk_fingerprint="r1",
        user_stream_ready=True,
        reconciliation_clean=True,
        ttl=timedelta(hours=1),
    )
    kwargs.update(over)
    return build_approval_artifact(**kwargs)


def test_exact_artifact_accepted() -> None:
    a = _art()
    validate_approval(
        a,
        commit_identity="abc123",
        config_fingerprint="cfg1",
        market_id="m1",
        instrument_token_id="tok1",
        quantity=Decimal("10"),
        limit_price=Decimal("0.50"),
        max_buy_notional=Decimal("5.00"),
        user_stream_ready=True,
        reconciliation_clean=True,
    )


@pytest.mark.parametrize(
    "field,kwargs,err",
    [
        ("market", {"market_id": "other"}, "MARKET_MISMATCH"),
        ("token", {"instrument_token_id": "other"}, "TOKEN_MISMATCH"),
        ("qty", {}, "QUANTITY_MISMATCH"),
        ("price", {}, "PRICE_MISMATCH"),
        ("config", {}, "CONFIG_FINGERPRINT_MISMATCH"),
        ("commit", {}, "CODE_IDENTITY_MISMATCH"),
    ],
)
def test_mismatches_rejected(field: str, kwargs: dict, err: str) -> None:
    a = _art()
    base = dict(
        commit_identity="abc123",
        config_fingerprint="cfg1",
        market_id="m1",
        instrument_token_id="tok1",
        quantity=Decimal("10"),
        limit_price=Decimal("0.50"),
        max_buy_notional=Decimal("5.00"),
        user_stream_ready=True,
        reconciliation_clean=True,
    )
    if field == "market":
        base["market_id"] = "other"
    elif field == "token":
        base["instrument_token_id"] = "other"
    elif field == "qty":
        base["quantity"] = Decimal("9")
    elif field == "price":
        base["limit_price"] = Decimal("0.51")
    elif field == "config":
        base["config_fingerprint"] = "other"
    elif field == "commit":
        base["commit_identity"] = "other"
    with pytest.raises(ApprovalError, match=err):
        validate_approval(a, **base)


def test_expired_and_replayed(tmp_path: Path) -> None:
    a = _art(ttl=timedelta(seconds=1))
    with pytest.raises(ApprovalError, match="EXPIRED"):
        validate_approval(
            a,
            commit_identity="abc123",
            config_fingerprint="cfg1",
            market_id="m1",
            instrument_token_id="tok1",
            quantity=Decimal("10"),
            limit_price=Decimal("0.50"),
            max_buy_notional=Decimal("5.00"),
            user_stream_ready=True,
            reconciliation_clean=True,
            now=datetime.now(timezone.utc) + timedelta(hours=2),
        )
    path = tmp_path / "a.json"
    write_approval(path, a)
    mark_consumed(path)
    consumed = read_approval(path)
    with pytest.raises(ApprovalError, match="CONSUMED"):
        validate_approval(
            consumed,
            commit_identity="abc123",
            config_fingerprint="cfg1",
            market_id="m1",
            instrument_token_id="tok1",
            quantity=Decimal("10"),
            limit_price=Decimal("0.50"),
            max_buy_notional=Decimal("5.00"),
            user_stream_ready=True,
            reconciliation_clean=True,
        )


def test_notional_over_5_rejected() -> None:
    with pytest.raises(ApprovalError, match="exceeds"):
        _art(max_buy_notional=Decimal("5.01"), quantity=Decimal("1"), limit_price=Decimal("5.01"))
