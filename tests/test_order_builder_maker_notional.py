"""BUY maker-notional quantization at OMS boundary (M8 validation hardening)."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent, ExitIntent, URGENCY_URGENT
from tyrex_pm.execution.order_builder import _quantize_buy_price_for_maker_notional, to_place_request


class _FakeMarketInfo:
    tick_size = Decimal("0.001")


def test_buy_price_aligned_to_two_decimal_maker_notional() -> None:
    price = _quantize_buy_price_for_maker_notional(
        Decimal("0.505"), Decimal("5"), Decimal("0.001")
    )
    assert (price * Decimal("5")).quantize(Decimal("0.01")) == price * Decimal("5")
    assert price == Decimal("0.506")


def test_to_place_request_buy_applies_maker_notional_quantization() -> None:
    intent = EnterIntent(
        token_id=TokenId("tok"),
        side=Side.BUY,
        size=Decimal("5"),
        limit_price=Decimal("0.505"),
        order_style=OrderStyle.FAK,
    )
    ap = ApprovedIntent(intent=intent, client_order_id=ClientOrderId("c1"), run_id=RunId("r1"))
    req = to_place_request(ap, market_info=_FakeMarketInfo())
    assert req.price == Decimal("0.506")
    assert req.price * req.size == Decimal("2.53")


def test_sell_price_unchanged_by_maker_notional_rule() -> None:
    intent = ExitIntent(
        token_id=TokenId("tok"),
        side=Side.SELL,
        size=Decimal("5"),
        limit_price=Decimal("0.505"),
        order_style=OrderStyle.FAK,
        urgency=URGENCY_URGENT,
    )
    ap = ApprovedIntent(intent=intent, client_order_id=ClientOrderId("c1"), run_id=RunId("r1"))
    req = to_place_request(ap, market_info=_FakeMarketInfo())
    assert req.price == Decimal("0.505")
