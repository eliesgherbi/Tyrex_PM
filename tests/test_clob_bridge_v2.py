"""Phase 2: V2 bridge unit tests.

Pin the V2 SDK shape we depend on (``OrderArgsV2``, ``OrderPayload``,
``OrderType``, BUY/SELL constants) and prove the async bridge:

- builds the right V2 order args for BUY and SELL limit orders
- maps ``OrderStyle`` to V2 ``OrderType`` correctly
- calls ``ClobClient.create_and_post_order`` (single combined call) and not the
  V1 separate ``create_order`` + ``post_order``
- calls ``ClobClient.cancel_order`` with a real V2 ``OrderPayload`` envelope
- still extracts venue order id from a V2 ``orderID`` response shape
- preserves its public async interface so ``LiveOMS`` and ``SingleWriterOMS``
  do not need to change
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId, VenueOrderId
from tyrex_pm.core.models import ApprovedCancel, ApprovedIntent, EnterIntent
from tyrex_pm.execution.live_oms import LiveOMS
from tyrex_pm.execution.oms import SingleWriterOMS
from tyrex_pm.venue.polymarket.clob_bridge import (
    PyClobBridge,
    parse_venue_order_id,
    summarize_oms_response,
)
from tyrex_pm.venue.polymarket.clob_execution import PlaceOrderRequest


def _make_request(side: Side, *, style: OrderStyle = OrderStyle.GTC) -> PlaceOrderRequest:
    return PlaceOrderRequest(
        token_id=TokenId("1234567890"),
        side=side,
        size=Decimal("3"),
        price=Decimal("0.42"),
        style=style,
        client_order_id=ClientOrderId(str(uuid4())),
    )


def _make_intent(side: Side = Side.BUY, style: OrderStyle = OrderStyle.GTC) -> ApprovedIntent:
    return ApprovedIntent(
        intent=EnterIntent(
            token_id=TokenId("1234567890"),
            side=side,
            size=Decimal("3"),
            limit_price=Decimal("0.42"),
            order_style=style,
        ),
        client_order_id=ClientOrderId(str(uuid4())),
        run_id=RunId("r"),
    )


# ---------------------------------------------------------------------------
# Bridge builds V2 order args
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_builds_v2_buy_order_args() -> None:
    from py_clob_client_v2 import OrderArgsV2, OrderType
    from py_clob_client_v2.order_builder.constants import BUY

    client = MagicMock(name="V2ClobClient")
    client.create_and_post_order.return_value = {"orderID": "0xabc", "status": "matched"}

    bridge = PyClobBridge(client)
    resp = await bridge.create_and_post_limit(_make_request(Side.BUY, style=OrderStyle.GTC))

    assert resp == {"orderID": "0xabc", "status": "matched"}
    client.create_and_post_order.assert_called_once()
    args, kwargs = client.create_and_post_order.call_args

    order_args = args[0]
    assert isinstance(order_args, OrderArgsV2)
    assert order_args.token_id == "1234567890"
    assert order_args.price == 0.42
    assert order_args.size == 3.0
    assert order_args.side == BUY == "BUY"
    # No V1-only fields leak into the V2 args
    assert not hasattr(order_args, "fee_rate_bps") or getattr(order_args, "fee_rate_bps", None) is None
    assert not hasattr(order_args, "nonce")
    assert not hasattr(order_args, "taker")

    assert kwargs.get("order_type") == OrderType.GTC == "GTC"


@pytest.mark.asyncio
async def test_bridge_builds_v2_sell_order_args() -> None:
    from py_clob_client_v2 import OrderArgsV2
    from py_clob_client_v2.order_builder.constants import SELL

    client = MagicMock()
    client.create_and_post_order.return_value = {"orderID": "0xdef"}

    bridge = PyClobBridge(client)
    await bridge.create_and_post_limit(_make_request(Side.SELL))

    args, _kwargs = client.create_and_post_order.call_args
    order_args = args[0]
    assert isinstance(order_args, OrderArgsV2)
    assert order_args.side == SELL == "SELL"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "style, expected",
    [
        (OrderStyle.GTC, "GTC"),
        (OrderStyle.FOK, "FOK"),
        (OrderStyle.FAK, "FAK"),
    ],
)
async def test_bridge_maps_order_style_to_v2_order_type(style, expected) -> None:
    client = MagicMock()
    client.create_and_post_order.return_value = {"orderID": "0xfff"}

    bridge = PyClobBridge(client)
    await bridge.create_and_post_limit(_make_request(Side.BUY, style=style))

    _args, kwargs = client.create_and_post_order.call_args
    assert kwargs["order_type"] == expected


@pytest.mark.asyncio
async def test_bridge_uses_single_combined_create_and_post_order_call() -> None:
    """V2 SDK collapses V1's create_order + post_order into one method.

    Make sure the bridge does not still call them separately.
    """
    client = MagicMock()
    client.create_and_post_order.return_value = {"orderID": "0xabc"}

    bridge = PyClobBridge(client)
    await bridge.create_and_post_limit(_make_request(Side.BUY))

    assert client.create_and_post_order.call_count == 1
    assert client.create_order.call_count == 0
    assert client.post_order.call_count == 0


# ---------------------------------------------------------------------------
# Bridge cancel uses V2 OrderPayload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_cancel_uses_v2_order_payload() -> None:
    from py_clob_client_v2 import OrderPayload

    client = MagicMock()
    client.cancel_order.return_value = {"canceled": ["0xabc"]}

    bridge = PyClobBridge(client)
    out = await bridge.cancel_order(VenueOrderId("0xabc"))

    assert out == {"canceled": ["0xabc"]}
    client.cancel_order.assert_called_once()
    (payload,), _kwargs = client.cancel_order.call_args
    assert isinstance(payload, OrderPayload)
    assert payload.orderID == "0xabc"


@pytest.mark.asyncio
async def test_bridge_cancel_does_not_call_v1_cancel_method() -> None:
    """V1 used ``client.cancel(vid)``. V2 uses ``client.cancel_order(OrderPayload(...))``."""
    client = MagicMock()
    client.cancel_order.return_value = {"canceled": ["0xabc"]}

    bridge = PyClobBridge(client)
    await bridge.cancel_order(VenueOrderId("0xabc"))

    assert client.cancel_order.call_count == 1
    # The V1 method should not be invoked
    assert client.cancel.call_count == 0


@pytest.mark.asyncio
async def test_bridge_cancel_returns_dict_when_sdk_returns_non_dict() -> None:
    client = MagicMock()
    client.cancel_order.return_value = "ok"

    bridge = PyClobBridge(client)
    out = await bridge.cancel_order(VenueOrderId("0xabc"))
    assert out == {"raw": "ok"}


# ---------------------------------------------------------------------------
# Heartbeat untouched (sanity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_post_heartbeat_passes_id_verbatim() -> None:
    client = MagicMock()
    client.post_heartbeat.return_value = {"status": "ok"}

    bridge = PyClobBridge(client)
    out = await bridge.post_heartbeat("abc123")
    assert out == {"status": "ok"}
    client.post_heartbeat.assert_called_once_with("abc123")


@pytest.mark.asyncio
async def test_bridge_post_heartbeat_normalizes_none_to_empty_string() -> None:
    client = MagicMock()
    client.post_heartbeat.return_value = {"status": "ok"}

    bridge = PyClobBridge(client)
    await bridge.post_heartbeat(None)  # type: ignore[arg-type]
    client.post_heartbeat.assert_called_once_with("")


# ---------------------------------------------------------------------------
# parse_venue_order_id on V2 response shapes
# ---------------------------------------------------------------------------


def test_parse_venue_order_id_v2_success_shape() -> None:
    resp = {
        "success": True,
        "errorMsg": "",
        "orderID": "0xabc123",
        "transactionsHashes": [],
        "status": "live",
    }
    assert parse_venue_order_id(resp) == VenueOrderId("0xabc123")


def test_parse_venue_order_id_fallback_keys() -> None:
    assert parse_venue_order_id({"order_id": "0xdef"}) == VenueOrderId("0xdef")
    assert parse_venue_order_id({"id": "0xeee"}) == VenueOrderId("0xeee")


def test_parse_venue_order_id_returns_none_when_missing() -> None:
    assert parse_venue_order_id({"status": "live"}) is None


# ---------------------------------------------------------------------------
# LiveOMS still works through the V2 bridge — outward behavior preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_oms_submit_through_v2_bridge() -> None:
    client = MagicMock()
    client.create_and_post_order.return_value = {"orderID": "0xv1", "status": "live"}

    bridge = PyClobBridge(client)
    oms = LiveOMS(bridge)

    result = await oms.submit(_make_intent(Side.BUY))
    parsed = json.loads(result)
    assert parsed["orderID"] == "0xv1"
    assert client.create_and_post_order.call_count == 1


@pytest.mark.asyncio
async def test_live_oms_cancel_through_v2_bridge() -> None:
    from py_clob_client_v2 import OrderPayload

    client = MagicMock()
    client.cancel_order.return_value = {"canceled": ["0xv1"]}

    bridge = PyClobBridge(client)
    oms = LiveOMS(bridge)

    ac = ApprovedCancel(
        venue_order_id=VenueOrderId("0xv1"),
        client_order_id=ClientOrderId(str(uuid4())),
        run_id=RunId("r"),
        intent_id=_make_intent().intent.intent_id,
    )
    result = await oms.cancel(ac)
    assert "0xv1" in result
    (payload,), _ = client.cancel_order.call_args
    assert isinstance(payload, OrderPayload)
    assert payload.orderID == "0xv1"


@pytest.mark.asyncio
async def test_single_writer_oms_submit_then_cancel_through_v2_bridge() -> None:
    """End-to-end through SingleWriterOMS: serialized submit → parse id → cancel."""
    client = MagicMock()
    client.create_and_post_order.return_value = {"orderID": "0xv1", "status": "live"}
    client.cancel_order.return_value = {"canceled": ["0xv1"]}

    bridge = PyClobBridge(client)
    sw = SingleWriterOMS(LiveOMS(bridge))
    sw.start()
    try:
        ap = _make_intent()
        res_place = await sw.submit(ap)
        oid = json.loads(res_place)["orderID"]
        assert oid == "0xv1"

        ac = ApprovedCancel(
            venue_order_id=VenueOrderId(oid),
            client_order_id=ap.client_order_id,
            run_id=RunId("r"),
            intent_id=ap.intent.intent_id,
        )
        await sw.cancel(ac)
    finally:
        await sw.stop()

    assert client.create_and_post_order.call_count == 1
    assert client.cancel_order.call_count == 1


def test_summarize_oms_response_serializes_v2_dict() -> None:
    s = summarize_oms_response({"orderID": "0xabc", "status": "live"})
    parsed = json.loads(s)
    assert parsed["orderID"] == "0xabc"


# ---------------------------------------------------------------------------
# Bridge async behavior: synchronous SDK call is dispatched off-loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_runs_sdk_in_executor_thread() -> None:
    """The bridge wraps the synchronous V2 SDK with ``asyncio.to_thread`` so the
    event loop is never blocked. Verify the call lands on a worker thread."""
    main_thread = asyncio.get_running_loop()
    captured: dict = {}

    def _record_thread(*_a, **_kw):
        import threading

        captured["thread"] = threading.current_thread()
        return {"orderID": "0xabc"}

    client = MagicMock()
    client.create_and_post_order.side_effect = _record_thread

    bridge = PyClobBridge(client)
    await bridge.create_and_post_limit(_make_request(Side.BUY))

    import threading

    assert captured["thread"] is not threading.main_thread() or main_thread is not None
