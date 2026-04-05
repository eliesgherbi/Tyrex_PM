#!/usr/bin/env python3
"""
Phase 0.5 C1 spike: connect to Polymarket RTDS, subscribe to ``activity`` / ``trades``, log rates.

Usage (from repo root):
  pip install -e .
  python scripts/spike_rtds_activity.py [--wallet 0x...] [--duration 60]

Optional ``--wallet`` enables client-side ``proxyWallet`` filtering for validation.
Does not submit orders.

Fill observations into ``Docs/Implementation/spike_C1_rtds_report.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default="wss://ws-live-data.polymarket.com",
        help="RTDS WebSocket URL",
    )
    parser.add_argument(
        "--wallet",
        default="",
        help="If set, only print payloads where proxyWallet matches (case-insensitive)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=60.0,
        help="Seconds to run before exit",
    )
    parser.add_argument(
        "--filtered-json",
        default="",
        help='Optional subscription filters JSON string, e.g. \'{"event_slug":"..."}\' (spike only)',
    )
    args = parser.parse_args()

    try:
        import websocket
    except ImportError:
        print("ERROR: pip install websocket-client", file=sys.stderr)
        return 1

    wallet = (args.wallet or "").strip().lower()
    stop = threading.Event()
    counts = {"n": 0, "matched": 0}
    t0 = time.monotonic()

    def normalize_wallet(a: str) -> str:
        return (a or "").strip().lower()

    def on_message(ws: websocket.WebSocketApp, message: str) -> None:  # noqa: ANN001
        if not message or message == "pong":
            return
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            print("non_json", repr(message[:200]))
            return
        if not isinstance(data, dict) or "payload" not in data:
            return
        counts["n"] += 1
        payload = data.get("payload")
        if not isinstance(payload, dict):
            return
        proxy = (
            payload.get("proxyWallet")
            or payload.get("proxy_wallet")
            or payload.get("user")
            or ""
        )
        proxy_n = normalize_wallet(str(proxy))
        if wallet and proxy_n != wallet:
            return
        counts["matched"] += 1
        if counts["matched"] <= 5 or counts["matched"] % 50 == 0:
            keys = sorted(payload.keys())
            print(
                json.dumps(
                    {
                        "n_total": counts["n"],
                        "n_matched": counts["matched"],
                        "keys": keys,
                        "proxyWallet": payload.get("proxyWallet"),
                        "transactionHash": payload.get("transactionHash"),
                        "asset": payload.get("asset"),
                        "side": payload.get("side"),
                    },
                    indent=2,
                ),
            )

    subs: list[dict] = [{"topic": "activity", "type": "trades"}]
    if args.filtered_json.strip():
        subs[0]["filters"] = args.filtered_json.strip()

    sub_msg = json.dumps({"action": "subscribe", "subscriptions": subs})

    def on_open(ws: websocket.WebSocketApp) -> None:  # noqa: ANN001
        print("open, sending:", sub_msg[:300])
        ws.send(sub_msg)

        def pings() -> None:
            while not stop.is_set():
                time.sleep(5.0)
                try:
                    ws.send("ping")
                except Exception:
                    break

        threading.Thread(target=pings, daemon=True).start()

    ws_app = websocket.WebSocketApp(
        args.url,
        on_open=on_open,
        on_message=on_message,
    )

    def closer() -> None:
        time.sleep(max(1.0, args.duration))
        stop.set()
        ws_app.close()

    threading.Thread(target=closer, daemon=True).start()
    try:
        ws_app.run_forever()
    except KeyboardInterrupt:
        stop.set()
    elapsed = time.monotonic() - t0
    print(
        f"done elapsed_s={elapsed:.1f} messages_total={counts['n']} matched={counts['matched']}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
