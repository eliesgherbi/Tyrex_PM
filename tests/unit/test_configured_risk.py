"""ConfiguredRiskPolicy fail-closed behavior."""

from __future__ import annotations

from tyrex_pm.config.loaders import RiskSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent
from tyrex_pm.risk.configured import ConfiguredRiskPolicy


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
