from __future__ import annotations

import asyncio
import json
import logging
import os
from decimal import Decimal
from typing import Any

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId, VenueOrderId
from tyrex_pm.core.models import OpenOrderView, TradeFillRecord
from tyrex_pm.core.time import utc_now
from tyrex_pm.execution.order_lifecycle import (
    apply_venue_open_order_to_local_orders,
    remove_local_resting_by_venue_order_id,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_confirmed_trade_to_wallet

log = logging.getLogger(__name__)

DEFAULT_USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


def _dec(x: Any) -> Decimal:
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")


def _apply_order_event(
    wallet: Any,
    msg: dict[str, Any],
    kind: str,
    order_store: OrderStore | None,
) -> None:
    oid = msg.get("id")
    if not oid:
        return
    if kind == "CANCELLATION":
        vid = VenueOrderId(str(oid))
        wallet.user_ws_remove_order(vid)
        if order_store is not None:
            remove_local_resting_by_venue_order_id(order_store, vid)
        return
    asset = msg.get("asset_id")
    if not asset:
        return
    orig_dec: Decimal | None = None
    matched_dec: Decimal | None = None
    if msg.get("original_size") is not None:
        orig_dec = _dec(msg["original_size"])
        matched_dec = _dec(msg.get("size_matched") or 0)
        rem = orig_dec - matched_dec
    elif msg.get("size") is not None:
        rem = _dec(msg["size"])
    else:
        return
    side_raw = str(msg.get("side", "BUY")).upper()
    side = Side.SELL if side_raw == "SELL" else Side.BUY
    view = OpenOrderView(
        token_id=TokenId(str(asset)),
        side=side,
        remaining_size=rem,
        limit_price=_dec(msg.get("price") or 0),
        client_order_id=None,
        venue_order_id=VenueOrderId(str(oid)),
        original_size=orig_dec,
        size_matched=matched_dec,
        venue_state_source="user_ws",
        order_status=str(msg.get("status") or "") or None,
    )
    wallet.user_ws_upsert_order(view)
    if order_store is not None:
        apply_venue_open_order_to_local_orders(order_store, view)


def _apply_trade(wallet: Any, msg: dict[str, Any]) -> None:
    asset = msg.get("asset_id")
    if not asset:
        return
    side_raw = str(msg.get("side", "BUY")).upper()
    side = Side.SELL if side_raw == "SELL" else Side.BUY
    sz = _dec(msg.get("size") or 0)
    px = _dec(msg.get("price") or 0)
    status = str(msg.get("status", "")).upper()
    if status in ("MATCHED", "MINED", "CONFIRMED") and sz > 0:
        wallet.record_user_ws_trade(
            TradeFillRecord(
                token_id=TokenId(str(asset)),
                side=side,
                size=sz,
                price=px,
                status=status,
                ts_utc=utc_now(),
                source="user_ws",
            )
        )
    if status == "CONFIRMED":
        apply_confirmed_trade_to_wallet(
            wallet,
            token_id=TokenId(str(asset)),
            side=side,
            size=sz,
            price=px,
        )


def apply_user_ws_message(
    wallet: Any,
    msg: dict[str, Any],
    order_store: OrderStore | None = None,
    coord: RuntimeCoordinator | None = None,
) -> None:
    """Apply one user-channel payload; optional ``order_store`` upgrades provisional local rows."""
    t = str(msg.get("type", "")).upper()
    if t == "TRADE":
        _apply_trade(wallet, msg)
        if coord is not None:
            from tyrex_pm.runtime.allocation_exit_lifecycle import process_user_ws_allocation_exit

            process_user_ws_allocation_exit(coord, msg)
        return
    if t in ("PLACEMENT", "UPDATE", "CANCELLATION"):
        _apply_order_event(wallet, msg, t, order_store)
        if coord is not None:
            from tyrex_pm.runtime.allocation_exit_lifecycle import process_user_ws_allocation_exit

            process_user_ws_allocation_exit(coord, msg)
        return
    if t in ("PONG", "PING", "SUBSCRIBED", "ERROR"):
        return


async def _ping_loop(ws: Any, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=10.0)
            return
        except asyncio.TimeoutError:
            try:
                await ws.send("PING")
            except Exception:
                log.exception("user ws ping failed")
                return


async def run_user_ws_ingest(
    coord: RuntimeCoordinator,
    *,
    api_key: str,
    secret: str,
    passphrase: str,
    stop: asyncio.Event,
    url: str | None = None,
) -> None:
    """Authenticated Polymarket user channel → wallet orders, trade ledger, confirmed fills → positions."""
    ws_url = (url or os.environ.get("TYREX_USER_WS_URL") or DEFAULT_USER_WS_URL).strip()
    sub_msg = json.dumps(
        {
            "type": "user",
            "auth": {"apiKey": api_key, "secret": secret, "passphrase": passphrase},
        }
    )
    try:
        import websockets
    except ImportError:
        log.error("websockets package required for user channel; pip install tyrex-pm[live]")
        await stop.wait()
        return

    while not stop.is_set():
        ping_stop = asyncio.Event()
        try:
            async with websockets.connect(ws_url, ping_interval=None) as ws:
                await ws.send(sub_msg)
                ping_task = asyncio.create_task(_ping_loop(ws, stop))
                try:
                    while not stop.is_set():
                        raw = await ws.recv()
                        coord.health.mark_user_ws_message(ts=utc_now())
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", errors="replace")
                        if raw == "PONG":
                            continue
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(msg, dict):
                            apply_user_ws_message(coord.wallet, msg, coord.orders, coord)
                            if coord.scheduled_exit_demo_try_arm is not None:
                                coord.scheduled_exit_demo_try_arm(source="websocket")
                finally:
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("user websocket session ended; reconnecting")
            try:
                await asyncio.wait_for(stop.wait(), timeout=3.0)
                return
            except asyncio.TimeoutError:
                continue
