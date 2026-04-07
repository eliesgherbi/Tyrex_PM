"""Phase B B3 — concurrent guru resting-order cap."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from tyrex_pm.config.loaders import (
    RiskSettings,
    load_runtime_settings,
    validate_phase_b_runtime_contract,
)
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent
from tyrex_pm.risk.configured import ConfiguredRiskPolicy
from tyrex_pm.runtime.state_readers import (
    GURU_ORDER_TAG_PREFIX,
    OrderSnapshot,
    POLYMARKET_VENUE_ID,
    is_guru_resting_order,
)


def _risk(**over) -> RiskSettings:
    r = RiskSettings(
        max_notional_usd_per_order=1000.0,
        max_token_notional_usd_open=float("inf"),
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
    )
    return replace(r, max_portfolio_notional_usd_open=float("inf"), **over)


def _intent() -> OrderIntent:
    return OrderIntent(
        correlation_id="c",
        token_id="t1",
        side="BUY",
        quantity=1.0,
        signal_kind="entry",
        reason_code="ok",
        price_ref=0.5,
    )


def _snap(**kwargs: object) -> OrderSnapshot:
    base: dict[str, object] = {
        "client_order_id": "X",
        "venue_order_id": "v",
        "status": "OPEN",
        "side": "BUY",
        "quantity": "1",
        "leaves_quantity": "1",
        "price": "0.5",
        "instrument_id": "0xcond-1.POLYMARKET",
        "tags": (),
    }
    base.update(kwargs)
    return OrderSnapshot(**base)  # type: ignore[arg-type]


def _tyrex_tx_id() -> str:
    """Valid tier-3 guru ``ClientOrderId`` body: ``TX`` + 26 hex chars."""
    return "TX" + "a" * 26


def test_cap_disabled_does_not_call_count() -> None:
    reader = MagicMock()
    pol = ConfiguredRiskPolicy(_risk(), execution_reader=reader)
    assert pol.evaluate(_intent())[0] is True
    reader.count_guru_resting_orders_open.assert_not_called()


def test_below_limit_allows() -> None:
    reader = MagicMock()
    reader.count_guru_resting_orders_open.return_value = 1
    pol = ConfiguredRiskPolicy(
        _risk(max_concurrent_guru_resting_orders=3),
        execution_reader=reader,
    )
    ok, rc, _ = pol.evaluate(_intent())
    assert ok is True
    assert rc == "approved"
    reader.count_guru_resting_orders_open.assert_called_once_with(venue=POLYMARKET_VENUE_ID)


def test_at_limit_denies(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="tyrex_pm.risk.configured")
    reader = MagicMock()
    reader.count_guru_resting_orders_open.return_value = 2
    pol = ConfiguredRiskPolicy(
        _risk(max_concurrent_guru_resting_orders=2),
        execution_reader=reader,
    )
    it = _intent()
    ok, rc, _ = pol.evaluate(it)
    assert ok is False
    assert rc == ReasonCode.RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT
    joined = " ".join(r.message for r in caplog.records)
    assert "tyrex_risk_ops" in joined
    assert "gate=guru_concurrent" in joined
    assert "guru_resting_count=2" in joined
    assert "limit=2" in joined
    assert it.correlation_id in joined


def test_above_limit_denies() -> None:
    reader = MagicMock()
    reader.count_guru_resting_orders_open.return_value = 5
    pol = ConfiguredRiskPolicy(
        _risk(max_concurrent_guru_resting_orders=2),
        execution_reader=reader,
    )
    ok, rc, _ = pol.evaluate(_intent())
    assert ok is False
    assert rc == ReasonCode.RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT


def test_no_execution_reader_denies_when_cap_on() -> None:
    pol = ConfiguredRiskPolicy(
        _risk(max_concurrent_guru_resting_orders=1),
        execution_reader=None,
    )
    ok, rc, _ = pol.evaluate(_intent())
    assert ok is False
    assert rc == ReasonCode.RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT


def test_is_guru_tier1_tag_matches_without_tx_shape() -> None:
    s = _snap(tags=(f"{GURU_ORDER_TAG_PREFIX}abc",), client_order_id="not-a-guru-id")
    assert is_guru_resting_order(s) is True


def test_is_guru_tier3_tx_prefix_isolated_in_helper() -> None:
    """Tier 3 documented fallback; never scatter raw ``TX`` checks in risk."""
    s = _snap(tags=(), client_order_id=_tyrex_tx_id())
    assert is_guru_resting_order(s) is True


def test_is_guru_non_guru_order() -> None:
    assert is_guru_resting_order(_snap(client_order_id="VENUE-MANUAL-1", tags=())) is False


def test_is_guru_tx_wrong_length_not_matched() -> None:
    assert is_guru_resting_order(_snap(client_order_id="TXabc", tags=())) is False


def test_b0_obsolete_framework_submit_yaml_rejected(tmp_path: Path) -> None:
    rt_path = tmp_path / "rt.yaml"
    rt_path.write_text(
        yaml.safe_dump(
            {
                "trader_id": "T-001",
                "execution_mode": "live",
                "polymarket_framework_submit": True,
                "polymarket_instrument_ids": ["0xabc-1.POLYMARKET"],
            },
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="obsolete"):
        load_runtime_settings(rt_path)
