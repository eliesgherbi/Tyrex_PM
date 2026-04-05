"""
Minimal Polymarket RTDS WebSocket client (``activity`` / ``trades``).

Wire format aligned with Polymarket ``real-time-data-client`` (subscribe + ``ping``).
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Callable
from queue import Queue
from typing import Any

LogFn = Callable[[str], None]


def default_subscribe_envelope_unfiltered() -> dict[str, Any]:
    """Unfiltered ``activity`` / ``trades`` subscription (v1 default)."""

    return {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "activity",
                "type": "trades",
            },
        ],
    }


class RtdsActivityTradesWorker:
    """
    Background thread: connect, subscribe, ``ping`` loop, push decoded messages to ``out_queue``.

    Queue items are ``dict`` messages (parsed JSON with ``payload``) or sentinel strings:
    ``"RECONNECT"``, ``"STALL"``.
    """

    __slots__ = (
        "_url",
        "_out",
        "_stop",
        "_ping_interval",
        "_liveness_timeout",
        "_subscribe_builder",
        "_log",
        "_thread",
        "_backoff_initial",
        "_backoff_max",
        "_ws_app",
        "_last_recv_monotonic",
        "_lock",
    )

    def __init__(
        self,
        url: str,
        out: Queue[Any],
        stop: threading.Event,
        *,
        ping_interval: float,
        liveness_timeout: float,
        reconnect_backoff_initial: float,
        reconnect_backoff_max: float,
        subscribe_envelope: dict[str, Any] | None,
        log: LogFn | None = None,
    ) -> None:
        self._url = url
        self._out = out
        self._stop = stop
        self._ping_interval = ping_interval
        self._liveness_timeout = liveness_timeout
        self._subscribe_builder = subscribe_envelope or default_subscribe_envelope_unfiltered()
        self._log = log or (lambda _s: None)
        self._backoff_initial = reconnect_backoff_initial
        self._backoff_max = reconnect_backoff_max
        self._thread: threading.Thread | None = None
        self._ws_app: Any = None
        self._last_recv_monotonic = 0.0
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_loop, name="tyrex_rtds", daemon=True)
        self._thread.start()

    def stop_join(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._lock:
            ws = self._ws_app
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _touch_recv(self) -> None:
        self._last_recv_monotonic = time.monotonic()

    def _run_loop(self) -> None:
        try:
            import websocket
        except ImportError as exc:
            self._log(f"event=guru_rtds_fatal detail=websocket_client_missing err={exc}")
            return

        attempt = 0
        while not self._stop.is_set():
            try:
                self._log("event=guru_rtds_connect_attempt")
                self._one_connection(websocket)
                attempt = 0
            except Exception as exc:
                attempt += 1
                delay = min(
                    self._backoff_max,
                    self._backoff_initial * (2 ** min(attempt, 8)) + random.random(),
                )
                self._log(
                    f"event=guru_rtds_reconnect scheduled_backoff_s={delay:.2f} err={exc!s} attempt={attempt}",
                )
                if self._stop.wait(timeout=delay):
                    break

    def _one_connection(self, websocket_module: Any) -> None:
        self._touch_recv()
        ping_pong: dict[str, threading.Event] = {"stop": threading.Event()}
        ws_holder: list[Any] = [None]

        def on_message(_ws: Any, message: str) -> None:
            if self._stop.is_set():
                return
            self._touch_recv()
            if not message:
                return
            if message == "pong":
                return
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                self._log(f"event=guru_rtds_parse_skip detail=invalid_json sample={message[:200]!r}")
                return
            if isinstance(data, dict) and "payload" in data:
                self._out.put(data)

        def on_error(_ws: Any, error: Any) -> None:
            self._log(f"event=guru_rtds_ws_error err={error!s}")

        def on_close(_ws: Any, status_code: Any, msg: Any) -> None:
            self._log(f"event=guru_rtds_ws_close code={status_code} msg={msg!s}")

        def on_open(ws: Any) -> None:
            ws_holder[0] = ws
            self._touch_recv()
            raw = json.dumps(self._subscribe_builder)
            ws.send(raw)
            self._log(f"event=guru_rtds_subscribed envelope={raw[:500]!s}")

            def ping_loop() -> None:
                while not self._stop.is_set() and not ping_pong["stop"].is_set():
                    if self._stop.wait(timeout=self._ping_interval):
                        break
                    try:
                        w = ws_holder[0]
                        if w is not None and w.sock and w.sock.connected:
                            w.send("ping")
                    except Exception as exc:
                        self._log(f"event=guru_rtds_ping_error err={exc!s}")
                        break

            threading.Thread(target=ping_loop, name="tyrex_rtds_ping", daemon=True).start()

        def run_liveness() -> None:
            while not self._stop.is_set():
                if self._stop.wait(timeout=min(5.0, self._liveness_timeout / 4)):
                    break
                idle = time.monotonic() - self._last_recv_monotonic
                if idle > self._liveness_timeout:
                    self._log(f"event=guru_rtds_stall idle_s={idle:.1f}")
                    try:
                        self._out.put("STALL")
                    except Exception:
                        pass
                    try:
                        w = ws_holder[0]
                        if w is not None:
                            w.close()
                    except Exception:
                        pass
                    ping_pong["stop"].set()
                    return

        threading.Thread(target=run_liveness, name="tyrex_rtds_liveness", daemon=True).start()

        self._ws_app = websocket_module.WebSocketApp(
            self._url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        with self._lock:
            app = self._ws_app

        try:
            app.run_forever(ping_interval=None, ping_timeout=None)
        finally:
            ping_pong["stop"].set()
            self._log("event=guru_rtds_disconnect")
            try:
                self._out.put("RECONNECT")
            except Exception:
                pass
