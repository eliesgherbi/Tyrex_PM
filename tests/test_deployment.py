from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import EnterIntent, OpenOrderView, RiskContext, WalletPosition
from tyrex_pm.risk.deployment import (
    RiskConfigCaps,
    check_deployment_caps,
    deployed_usd_for_token,
)


def test_deployed_token_includes_open_buy() -> None:
    t = TokenId("1")
    positions = {t: WalletPosition(token_id=t, qty=Decimal("10"), avg_price_usd=Decimal("0.5"))}
    marks = {t: Decimal("0.5")}
    orders = (
        OpenOrderView(
            token_id=t,
            side=Side.BUY,
            remaining_size=Decimal("10"),
            limit_price=Decimal("0.6"),
            client_order_id=None,
            venue_order_id=None,
        ),
    )
    d, unk = deployed_usd_for_token(
        token_id=t, positions=positions, open_orders=orders, mark_prices=marks
    )
    assert not unk
    assert d == Decimal("5") + Decimal("6")  # 10*0.5 + 10*0.6


def test_cap_denies() -> None:
    t = TokenId("1")
    positions = {t: WalletPosition(token_id=t, qty=Decimal("100"), avg_price_usd=Decimal("1"))}
    marks = {t: Decimal("1")}
    ctx = RiskContext(
        execution_mode=ExecutionMode.SHADOW,
        wallet_positions=tuple(positions.values()),
        open_orders=(),
        usdc_balance=Decimal("100000"),
        usdc_allowance=Decimal("100000"),
        last_wallet_sync_ts=None,
        mark_prices=marks,
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
    )
    caps = RiskConfigCaps(token_cap_usd=Decimal("50"), portfolio_cap_usd=Decimal("1000"))
    ok, reason = check_deployment_caps(caps, ctx)
    assert not ok
    from tyrex_pm.core import reason_codes as rc

    assert reason == rc.TOKEN_DEPLOYMENT_CAP


def test_projected_buy_reserves_new_token() -> None:
    """Hypothetical BUY must count toward token + portfolio caps (no existing position)."""
    t = TokenId("99")
    ctx = RiskContext(
        execution_mode=ExecutionMode.SHADOW,
        wallet_positions=(),
        open_orders=(),
        usdc_balance=None,
        usdc_allowance=None,
        last_wallet_sync_ts=None,
        mark_prices={t: Decimal("0.5")},
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
        reconcile_drift=False,
    )
    intent = EnterIntent(
        token_id=t,
        side=Side.BUY,
        size=Decimal("200"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    caps = RiskConfigCaps(token_cap_usd=Decimal("90"), portfolio_cap_usd=Decimal("1000"))
    ok, reason = check_deployment_caps(caps, ctx, pending_intent=intent)
    assert not ok
    from tyrex_pm.core import reason_codes as rc

    assert reason == rc.TOKEN_DEPLOYMENT_CAP
