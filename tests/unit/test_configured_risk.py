"""ConfiguredRiskPolicy fail-closed behavior."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tyrex_pm.config.loaders import RiskSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent
from tyrex_pm.risk.configured import ConfiguredRiskPolicy
from tyrex_pm.runtime.deployment_budget import NautilusDeploymentBudget
from tyrex_pm.runtime.state_readers import OrderSnapshot


def _intent(qty: float = 1.0, price: float | None = 0.5, token: str = "tok") -> OrderIntent:
    return OrderIntent(
        correlation_id="c1",
        token_id=token,
        side="BUY",
        quantity=qty,
        signal_kind="entry",
        reason_code="ok",
        price_ref=price,
    )


def _sell_intent(qty: float = 1.0, price: float | None = 0.5, token: str = "tok") -> OrderIntent:
    return OrderIntent(
        correlation_id="c-sell",
        token_id=token,
        side="SELL",
        quantity=qty,
        signal_kind="exit",
        reason_code="ok",
        price_ref=price,
    )


def _budget_for_reader(reader: MagicMock) -> NautilusDeploymentBudget:
    poly = MagicMock()
    poly.is_flat.return_value = True
    cache = MagicMock()
    cache.positions_open.return_value = ()
    return NautilusDeploymentBudget(
        poly,
        cache,
        reader,
        {"88888": "0xabc-88888.POLYMARKET", "tok": "0xabc-88888.POLYMARKET"},
    )


def test_min_notional_usd_per_order_buy_denies_below_floor() -> None:
    s = RiskSettings(
        max_notional_usd_per_order=1000.0,
        max_token_notional_usd_open=float("inf"),
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
        min_notional_usd_per_order=1.0,
    )
    pol = ConfiguredRiskPolicy(s)
    ok, rc, out = pol.evaluate(_intent(qty=1.0, price=0.5))
    assert ok is False
    assert rc == ReasonCode.RISK_MIN_ORDER_NOTIONAL
    assert out is None
    ok2, rc2, out2 = pol.evaluate(_intent(qty=3.0, price=0.5))
    assert ok2 is True
    assert rc2 == "approved"
    assert out2 is not None
    assert out2.quantity == pytest.approx(3.0)


def test_kill_switch() -> None:
    s = RiskSettings(
        max_notional_usd_per_order=1000.0,
        max_token_notional_usd_open=float("inf"),
        kill_switch=True,
        fail_on_missing_price_for_notional=True,
    )
    pol = ConfiguredRiskPolicy(s)
    ok, rc, _out = pol.evaluate(_intent())
    assert ok is False
    assert rc == ReasonCode.RISK_KILL_SWITCH


def test_pending_notional_from_cache() -> None:
    s = RiskSettings(
        max_notional_usd_per_order=1000.0,
        max_token_notional_usd_open=15.0,
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
    )
    reader = MagicMock()
    reader.list_open_orders.return_value = (
        OrderSnapshot(
            client_order_id="c1",
            venue_order_id="v",
            status="PARTIALLY_FILLED",
            side="BUY",
            quantity="10",
            leaves_quantity="4",
            price="0.5",
            instrument_id="0xabc-88888.POLYMARKET",
        ),
    )
    reader.count_guru_resting_orders_open = MagicMock(return_value=0)
    db = _budget_for_reader(reader)
    pol = ConfiguredRiskPolicy(
        s,
        execution_reader=reader,
        deployment_budget=db,
    )
    ok, rc, _out = pol.evaluate(_intent(qty=10.0, price=1.1, token="88888"))
    assert ok is True

    ok, rc, _out = pol.evaluate(_intent(qty=10.0, price=1.31, token="88888"))
    assert ok is False
    assert rc == ReasonCode.RISK_TOKEN_DEPLOYMENT_EXCEEDED


def test_framework_open_order_count_uses_injected_reader() -> None:
    s = RiskSettings(
        max_notional_usd_per_order=1000.0,
        max_token_notional_usd_open=float("inf"),
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
    )
    reader = MagicMock()
    reader.list_open_orders.return_value = (1, 2, 3)
    reader.count_guru_resting_orders_open = MagicMock(return_value=0)
    pol = ConfiguredRiskPolicy(s, execution_reader=reader)
    assert pol.framework_open_order_count() == 3


def test_token_open_cap_uses_cache_pending() -> None:
    s = RiskSettings(
        max_notional_usd_per_order=1000.0,
        max_token_notional_usd_open=15.0,
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
    )
    reader = MagicMock()
    reader.list_open_orders.return_value = (
        OrderSnapshot(
            client_order_id="c0",
            venue_order_id="v",
            status="ACCEPTED",
            side="BUY",
            quantity="10",
            leaves_quantity="10",
            price="0.5",
            instrument_id="0xabc-88888.POLYMARKET",
        ),
    )
    reader.count_guru_resting_orders_open = MagicMock(return_value=0)
    db = _budget_for_reader(reader)
    pol = ConfiguredRiskPolicy(s, execution_reader=reader, deployment_budget=db)
    ok, rc, _out = pol.evaluate(_intent(qty=10.0, price=1.2, token="88888"))
    assert ok is False
    assert rc == ReasonCode.RISK_TOKEN_DEPLOYMENT_EXCEEDED


def test_max_notional_policy_cap_clips_qty() -> None:
    s = RiskSettings(
        max_notional_usd_per_order=5.0,
        max_token_notional_usd_open=float("inf"),
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
        max_notional_policy="cap",
    )
    pol = ConfiguredRiskPolicy(s)
    ok, rc, out = pol.evaluate(_intent(qty=100.0, price=0.1))
    assert ok is True
    assert out is not None
    assert out.quantity == pytest.approx(50.0)
    assert out.quantity * 0.1 <= 5.0 + 1e-6


def test_sell_skips_open_deployment_caps_when_buy_would_breach() -> None:
    """SELL bypasses only additive open caps when inventory gate passes (Scenario A closes)."""
    s = RiskSettings(
        max_notional_usd_per_order=1000.0,
        max_token_notional_usd_open=5.0,
        max_portfolio_notional_usd_open=5.0,
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
    )
    db = MagicMock()
    db.filled_usd_for_token.return_value = (3.0, True)
    db.token_deployment_usd_with_policy.return_value = (5.0, True, None)
    db.portfolio_deployment_usd_with_policy.return_value = (5.0, True, None)
    reader = MagicMock()
    reader.list_open_orders.return_value = ()
    reader.count_guru_resting_orders_open = MagicMock(return_value=0)
    pol = ConfiguredRiskPolicy(s, execution_reader=reader, deployment_budget=db)

    ok_buy, rc_buy, _o = pol.evaluate(_intent(qty=2.0, price=0.6))
    assert ok_buy is False
    assert rc_buy == ReasonCode.RISK_TOKEN_DEPLOYMENT_EXCEEDED

    ok_sell, rc_sell, out_s = pol.evaluate(_sell_intent(qty=2.0, price=0.6))
    assert ok_sell is True
    assert rc_sell == "approved"
    assert out_s is not None


def test_sell_denied_without_filled_inventory() -> None:
    s = RiskSettings(
        max_notional_usd_per_order=1000.0,
        max_token_notional_usd_open=5.0,
        max_portfolio_notional_usd_open=5.0,
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
    )
    db = MagicMock()
    db.filled_usd_for_token.return_value = (0.0, True)
    reader = MagicMock()
    reader.list_open_orders.return_value = ()
    reader.count_guru_resting_orders_open = MagicMock(return_value=0)
    pol = ConfiguredRiskPolicy(s, execution_reader=reader, deployment_budget=db)
    ok, rc, _ = pol.evaluate(_sell_intent(qty=1.0, price=0.5))
    assert ok is False
    assert rc == ReasonCode.RISK_SELL_WITHOUT_FILLED_INVENTORY


def test_sell_denied_when_order_deploy_exceeds_filled_usd() -> None:
    s = RiskSettings(
        max_notional_usd_per_order=1000.0,
        max_token_notional_usd_open=5.0,
        max_portfolio_notional_usd_open=5.0,
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
    )
    db = MagicMock()
    db.filled_usd_for_token.return_value = (1.0, True)
    reader = MagicMock()
    reader.list_open_orders.return_value = ()
    reader.count_guru_resting_orders_open = MagicMock(return_value=0)
    pol = ConfiguredRiskPolicy(s, execution_reader=reader, deployment_budget=db)
    ok, rc, _ = pol.evaluate(_sell_intent(qty=10.0, price=0.5))
    assert ok is False
    assert rc == ReasonCode.RISK_SELL_EXCEEDS_FILLED_INVENTORY


def test_min_notional_policy_cap_bumps_qty() -> None:
    s = RiskSettings(
        max_notional_usd_per_order=100.0,
        max_token_notional_usd_open=float("inf"),
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
        min_notional_usd_per_order=5.0,
        min_notional_policy="cap",
        max_notional_policy="deny",
    )
    pol = ConfiguredRiskPolicy(s)
    ok, rc, out = pol.evaluate(_intent(qty=2.0, price=0.5))
    assert ok is True
    assert out is not None
    assert out.quantity == pytest.approx(10.0)
    assert out.quantity * 0.5 >= 5.0 - 1e-6
