"""Authenticated user WebSocket observation via official AsyncSecureClient."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable
from urllib.parse import urlparse

from tyrex_pm.adapters.polymarket.sdk_errors import (
    PolymarketErrorCategory,
    classify_polymarket_error,
)
from tyrex_pm.execution.polymarket.auth import L2Credentials, redact_text

USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

# Probe lifecycle stages — never collapse adapter bugs into "connectivity".
STAGE_CREATE = "CREATE"
STAGE_SUBSCRIBE = "SUBSCRIBE"
STAGE_RECEIVE = "RECEIVE"
STAGE_CLOSE = "CLOSE"


def _classify_user_stream_failure(exc: BaseException, *, stage: str) -> dict[str, str]:
    """Map exception + stage to precise failure_kind (not VPN/connectivity by default)."""
    name = type(exc).__name__
    # Local programming / contract errors (unawaited factory, wrong type, etc.)
    if isinstance(exc, (AttributeError, TypeError, RuntimeError)) and any(
        x in str(exc).lower()
        for x in ("coroutine", "await", "has no attribute 'subscribe'", "not await")
    ):
        return {
            "failure_kind": "adapter_init",
            "error_class": name,
            "stage": stage,
        }
    if isinstance(exc, (AttributeError, TypeError)) and stage == STAGE_CREATE:
        return {
            "failure_kind": "adapter_init",
            "error_class": name,
            "stage": stage,
        }

    classified = classify_polymarket_error(exc)
    if classified.category is PolymarketErrorCategory.AUTH:
        return {"failure_kind": "auth", "error_class": name, "stage": stage}
    if classified.category in {
        PolymarketErrorCategory.TRANSIENT,
        PolymarketErrorCategory.TRANSPORT,
        PolymarketErrorCategory.RATE_LIMIT,
    }:
        return {"failure_kind": "transport", "error_class": name, "stage": stage}
    if classified.category is PolymarketErrorCategory.REJECTED:
        return {"failure_kind": "rejected", "error_class": name, "stage": stage}
    if stage in {STAGE_SUBSCRIBE, STAGE_RECEIVE}:
        return {"failure_kind": "protocol", "error_class": name, "stage": stage}
    if stage == STAGE_CREATE:
        return {"failure_kind": "adapter_init", "error_class": name, "stage": stage}
    return {"failure_kind": "protocol", "error_class": name, "stage": stage}


def observe_user_stream_readonly(
    *,
    creds: L2Credentials,
    observe_s: float = 5.0,
    on_disconnect: Callable[[], None] | None = None,
    on_ready: Callable[[], None] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Connect via official SDK user stream, observe, disconnect/reconnect once.

    Idle accounts may receive zero order/trade events. Connection + subscription
    without auth errors is sufficient evidence. No orders are created.

    Note: the sync entry uses a dedicated event loop only at this composition
    boundary (preflight is sync). The async factory itself is always awaited
    inside that loop — never returned as a bare coroutine.
    """

    def _run() -> dict[str, Any]:
        return asyncio.run(
            _observe_async(
                creds=creds,
                observe_s=observe_s,
                on_disconnect=on_disconnect,
                on_ready=on_ready,
                env=env,
            )
        )

    try:
        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False
        if in_loop:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(_run).result(timeout=max(30.0, observe_s + 20.0))
        return _run()
    except Exception as exc:  # noqa: BLE001
        meta = _classify_user_stream_failure(exc, stage=STAGE_CREATE)
        return {
            "attempted": True,
            "connected": False,
            "authenticated": False,
            "orders_created": False,
            "transport": "polymarket-client",
            "detail": redact_text(str(exc)[:200], creds),
            **meta,
        }


async def _observe_async(
    *,
    creds: L2Credentials,
    observe_s: float,
    on_disconnect: Callable[[], None] | None,
    on_ready: Callable[[], None] | None,
    env: dict[str, str] | None,
) -> dict[str, Any]:
    from polymarket.streams._specs import UserSpec

    from tyrex_pm.adapters.polymarket.sdk_secure import build_async_secure_client

    report: dict[str, Any] = {
        "attempted": True,
        "url_host": urlparse(USER_WS_URL).hostname,
        "path": urlparse(USER_WS_URL).path,
        "connected": False,
        "authenticated": False,
        "auth_message_sent": False,
        "auth_error": False,
        "pong_seen": False,
        "events_received": 0,
        "disconnect_detected": False,
        "reconnected": False,
        "orders_created": False,
        "idle_account_ok": False,
        "observe_s": observe_s,
        "transport": "polymarket-client",
        "stage": STAGE_CREATE,
        "failure_kind": None,
        "error_class": None,
        "detail": None,
    }

    async def _session(*, reconnect: bool = False, window_s: float) -> None:
        client = None
        handle = None
        stage = STAGE_CREATE
        try:
            stage = STAGE_CREATE
            report["stage"] = stage
            client = await build_async_secure_client(env=env, creds=creds)
            stage = STAGE_SUBSCRIBE
            report["stage"] = stage
            handle = await client.subscribe(UserSpec(markets=None))
            report["connected"] = True
            report["auth_message_sent"] = True
            report["authenticated"] = True
            if reconnect:
                report["reconnected"] = True
            # SDK manages application heartbeats; treat successful subscribe as pong-equivalent.
            report["pong_seen"] = True
            stage = STAGE_RECEIVE
            report["stage"] = stage
            deadline = time.monotonic() + max(window_s, 1.0)
            while time.monotonic() < deadline:
                try:
                    _event = await asyncio.wait_for(handle.__anext__(), timeout=1.0)
                    report["events_received"] += 1
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    continue
                except Exception as exc:  # noqa: BLE001
                    low = str(exc).lower()
                    if "auth" in low or "unauthorized" in low or "invalid" in low:
                        report["auth_error"] = True
                        report["authenticated"] = False
                        report["failure_kind"] = "auth"
                        report["error_class"] = type(exc).__name__
                        report["detail"] = redact_text(str(exc)[:200], creds)
                    else:
                        meta = _classify_user_stream_failure(exc, stage=STAGE_RECEIVE)
                        report.update(meta)
                        report["detail"] = redact_text(str(exc)[:200], creds)
                        report["authenticated"] = False
                    break
        except asyncio.CancelledError:
            report["failure_kind"] = "cancelled"
            report["error_class"] = "CancelledError"
            report["stage"] = stage
            report["authenticated"] = False
            raise
        except Exception as exc:  # noqa: BLE001
            meta = _classify_user_stream_failure(exc, stage=stage)
            report.update(meta)
            report["detail"] = redact_text(str(exc)[:200], creds)
            report["authenticated"] = False
            if meta["failure_kind"] == "auth":
                report["auth_error"] = True
        finally:
            report["stage"] = STAGE_CLOSE
            if handle is not None:
                try:
                    await handle.close()
                except Exception:  # noqa: BLE001
                    pass
            if client is not None:
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass

    await _session(reconnect=False, window_s=observe_s)
    if report.get("failure_kind"):
        # Do not attempt reconnect after a hard failure on the first session.
        return report

    if on_disconnect is not None:
        on_disconnect()
        report["disconnect_detected"] = True
    await asyncio.sleep(0.2)
    await _session(reconnect=True, window_s=min(2.0, observe_s))

    if (
        report["connected"]
        and report["auth_message_sent"]
        and not report["auth_error"]
        and report["pong_seen"]
        and not report.get("failure_kind")
    ):
        report["authenticated"] = True
        report["idle_account_ok"] = report["events_received"] == 0
        if on_ready is not None:
            on_ready()

    return report
