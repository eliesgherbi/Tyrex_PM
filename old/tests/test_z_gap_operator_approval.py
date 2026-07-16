"""Operator approval validation tests (A0.8)."""

from __future__ import annotations

import time

from tyrex_pm.runtime.z_gap_live_preflight import validate_operator_approval


def test_approval_market_scoped() -> None:
    errs = validate_operator_approval(
        {"approved": True, "market_id": "btc_5m_20260709_1200", "maximum_usd": "5", "one_trade_only": True, "expiration_ts": time.time() + 3600},
        market_id="btc_5m_20260709_1200",
    )
    assert not errs


def test_approval_wrong_market_blocks() -> None:
    errs = validate_operator_approval(
        {"approved": True, "market_id": "btc_5m_other", "maximum_usd": "5", "one_trade_only": True, "expiration_ts": time.time() + 3600},
        market_id="btc_5m_20260709_1200",
    )
    assert any("scoped" in e for e in errs)


def test_expired_approval_blocks() -> None:
    errs = validate_operator_approval(
        {"approved": True, "market_id": "m1", "maximum_usd": "5", "one_trade_only": True, "expiration_ts": time.time() - 10},
        market_id="m1",
    )
    assert any("expired" in e for e in errs)


def test_approval_over_cap_blocks() -> None:
    errs = validate_operator_approval(
        {"approved": True, "market_id": "m1", "maximum_usd": "10", "one_trade_only": True, "expiration_ts": time.time() + 3600},
        market_id="m1",
    )
    assert any("maximum_usd" in e for e in errs)
