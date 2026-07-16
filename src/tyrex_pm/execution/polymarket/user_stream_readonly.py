"""Authenticated user WebSocket observation — read-only, no order creation."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable
from urllib.parse import urlparse

from tyrex_pm.execution.polymarket.auth import L2Credentials, redact_text

USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


def observe_user_stream_readonly(
    *,
    creds: L2Credentials,
    observe_s: float = 5.0,
    on_disconnect: Callable[[], None] | None = None,
    on_ready: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Connect, authenticate, observe, disconnect/reconnect once. No orders created."""
    try:
        return asyncio.run(
            _observe_async(
                creds=creds,
                observe_s=observe_s,
                on_disconnect=on_disconnect,
                on_ready=on_ready,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "attempted": True,
            "connected": False,
            "authenticated": False,
            "error_class": type(exc).__name__,
            "detail": redact_text(str(exc)[:200], creds),
            "orders_created": False,
        }


async def _observe_async(
    *,
    creds: L2Credentials,
    observe_s: float,
    on_disconnect: Callable[[], None] | None,
    on_ready: Callable[[], None] | None,
) -> dict[str, Any]:
    import websockets

    report: dict[str, Any] = {
        "attempted": True,
        "url_host": urlparse(USER_WS_URL).hostname,
        "path": urlparse(USER_WS_URL).path,
        "connected": False,
        "authenticated": False,
        "pong_seen": False,
        "events_received": 0,
        "disconnect_detected": False,
        "reconnected": False,
        "orders_created": False,
        "observe_s": observe_s,
    }

    auth_msg = {
        "auth": {
            "apiKey": creds.api_key,
            "secret": creds.secret,
            "passphrase": creds.passphrase,
        },
        "type": "user",
        "markets": [],
    }

    async def _session(*, reconnect: bool = False) -> None:
        async with websockets.connect(USER_WS_URL, open_timeout=15, close_timeout=5) as ws:
            report["connected"] = True
            if reconnect:
                report["reconnected"] = True
            await ws.send(json.dumps(auth_msg))
            # Official channel: auth then subscribe; idle accounts may emit no trades.
            deadline = time.monotonic() + max(observe_s, 1.0)
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except TimeoutError:
                    # Protocol ping
                    try:
                        pong = await ws.ping()
                        await asyncio.wait_for(pong, timeout=2.0)
                        report["pong_seen"] = True
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                report["events_received"] += 1
                text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
                low = text.lower()
                if "error" in low and ("auth" in low or "invalid" in low):
                    report["authenticated"] = False
                    break
                # Treat non-error frames after auth send as auth-success for idle accounts
                report["authenticated"] = True
                if on_ready is not None:
                    on_ready()

    await _session(reconnect=False)
    if on_disconnect is not None:
        on_disconnect()
        report["disconnect_detected"] = True
    await asyncio.sleep(0.2)
    await _session(reconnect=True)
    if report["authenticated"] and on_ready is not None:
        on_ready()
    return report
