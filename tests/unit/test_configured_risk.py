"""ConfiguredRiskPolicy fail-closed behavior."""

from __future__ import annotations

from unittest.mock import MagicMock

from tyrex_pm.config.loaders import RiskSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent
from tyrex_pm.risk.configured import ConfiguredRiskPolicy
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


def test_kill_switch() -> None:
    s = RiskSettings(
        max_order_quantity=100.0,
        max_notional_usd_per_order=1000.0,
        max_token_notional_usd_open=float("inf"),
        kill_switch=True,
        fail_on_missing_price_for_notional=True,
    )
    pol = ConfiguredRiskPolicy(s)
    ok, rc = pol.evaluate(_intent())
    assert ok is False
    assert rc == ReasonCode.RISK_KILL_SWITCH


def test_note_fill_noop_when_token_open_not_authoritative() -> None:
    s = RiskSettings(
        max_order_quantity=100.0,
        max_notional_usd_per_order=1000.0,
        max_token_notional_usd_open=15.0,
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
    )
    pol = ConfiguredRiskPolicy(s, token_open_authoritative_for_pending=False)
    pol.note_fill_assumption(_intent(qty=10.0, price=0.5))
    # Would be 5 notional; second intent 10 more would exceed 15 if _token_open counted
    ok, _ = pol.evaluate(_intent(qty=10.0, price=0.5))
    assert ok is True


def test_pending_notional_from_cache_when_not_token_open_authoritative() -> None:
    s = RiskSettings(
        max_order_quantity=100.0,
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
    pol = ConfiguredRiskPolicy(
        s,
        execution_reader=reader,
        token_open_authoritative_for_pending=False,
    )
    # leaves 4 * 0.5 = 2 pending + 10 * 1.1 = 13 <= 15
    ok, rc = pol.evaluate(_intent(qty=10.0, price=1.1, token="88888"))
    assert ok is True

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
    # 2 pending + 10 * 1.31 = 15.1 > 15
    ok, rc = pol.evaluate(_intent(qty=10.0, price=1.31, token="88888"))
    assert ok is False
    assert rc == ReasonCode.RISK_TOKEN_NOTIONAL_OPEN


def test_framework_open_order_count_uses_injected_reader() -> None:
    s = RiskSettings(
        max_order_quantity=100.0,
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


def test_exposure_tracking() -> None:
    s = RiskSettings(
        max_order_quantity=100.0,
        max_notional_usd_per_order=10.0,
        max_token_notional_usd_open=15.0,
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
    )
    pol = ConfiguredRiskPolicy(s)
    i1 = _intent(qty=10.0, price=0.5)  # notional 5
    assert pol.evaluate(i1)[0] is True
    pol.note_fill_assumption(i1)
    i2 = _intent(qty=24.0, price=0.5)  # +12 -> 17 > 15
    assert pol.evaluate(i2)[0] is False
