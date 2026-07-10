#!/usr/bin/env python3
"""A0.0.b — Binance WS reachability preflight for Z-Gap Phase A.

Checks whether Binance combined-stream WS is reachable from the current host
and returns timing evidence for operator review before live Z-Gap runs.

Usage:
    python scripts/preflight_binance_connectivity.py
    python scripts/preflight_binance_connectivity.py --output var/reporting/z_gap/binance_connectivity.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tyrex_pm.venue.binance_data.ws_client import (
    DEFAULT_BINANCE_WS_BASE,
    BinanceDataWsClient,
    build_combined_stream_url,
    parse_binance_ws_message,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BinanceConnectivityReport:
    ok: bool
    symbol: str
    streams: list[str]
    ws_url: str
    ws_base: str
    checked_at_utc: str
    connect_ms: float | None = None
    first_message_ms: float | None = None
    first_stream: str | None = None
    first_event: str | None = None
    reconnect_ok: bool | None = None
    reconnect_ms: float | None = None
    error: str | None = None
    recommendation: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _recommendation(ok: bool, reconnect_ok: bool | None) -> str:
    if not ok:
        return (
            "Binance WS unreachable from this host. Do not enable Z-Gap entry_mode: enforce "
            "until an approved substitute fast-BTC feed path is defined (e.g. RTDS Binance topic)."
        )
    if reconnect_ok is False:
        return (
            "First connect succeeded but reconnect probe failed. Observe_only may proceed; "
            "review network stability before enforce."
        )
    return "Binance WS reachable. Proceed with Z-Gap Phase A feed wiring (A0.2)."


async def _wait_first_message(
    ws,
    *,
    deadline: float,
) -> tuple[float, str | None, str | None, dict[str, Any] | None]:
    """Return (elapsed_ms, stream, event_type, data) for first parsed message."""
    started = time.perf_counter()
    while time.perf_counter() < deadline:
        remaining = deadline - time.perf_counter()
        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
        stream_name, data = parse_binance_ws_message(raw)
        if data is None:
            continue
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        event = str(data.get("e") or stream_name or "")
        return elapsed_ms, stream_name, event, data
    raise TimeoutError("no Binance message before deadline")


async def run_check(
    *,
    symbol: str,
    streams: tuple[str, ...],
    ws_base: str,
    connect_timeout_s: float,
    first_message_timeout_s: float,
    test_reconnect: bool,
) -> BinanceConnectivityReport:
    try:
        import websockets
    except ImportError as exc:
        return BinanceConnectivityReport(
            ok=False,
            symbol=symbol,
            streams=list(streams),
            ws_url=build_combined_stream_url(symbol, streams, ws_base=ws_base),
            ws_base=ws_base,
            checked_at_utc=_utc_now().isoformat(),
            error=f"websockets package missing: {exc!r}",
            recommendation=_recommendation(False, None),
        )

    client = BinanceDataWsClient(symbol=symbol, streams=streams, ws_base=ws_base)
    report = BinanceConnectivityReport(
        ok=False,
        symbol=client.symbol,
        streams=list(client.streams),
        ws_url=client.ws_url,
        ws_base=ws_base,
        checked_at_utc=_utc_now().isoformat(),
    )

    try:
        connect_started = time.perf_counter()
        async with websockets.connect(
            client.ws_url,
            ping_interval=20,
            open_timeout=connect_timeout_s,
        ) as ws:
            report.connect_ms = round((time.perf_counter() - connect_started) * 1000.0, 2)
            deadline = time.perf_counter() + first_message_timeout_s
            first_ms, stream, event, data = await _wait_first_message(ws, deadline=deadline)
            report.first_message_ms = round(first_ms, 2)
            report.first_stream = stream
            report.first_event = event
            if data is not None:
                report.extra["first_payload_keys"] = sorted(data.keys())
                if "E" in data:
                    report.extra["first_event_time_ms"] = data.get("E")
                if "b" in data and "a" in data:
                    report.extra["first_bid"] = data.get("b")
                    report.extra["first_ask"] = data.get("a")

        report.ok = True

        if test_reconnect:
            reconnect_started = time.perf_counter()
            async with websockets.connect(
                client.ws_url,
                ping_interval=20,
                open_timeout=connect_timeout_s,
            ) as ws2:
                deadline = time.perf_counter() + first_message_timeout_s
                await _wait_first_message(ws2, deadline=deadline)
                report.reconnect_ms = round((time.perf_counter() - reconnect_started) * 1000.0, 2)
                report.reconnect_ok = True

    except Exception as exc:
        report.error = f"{type(exc).__name__}: {exc}"
        report.ok = False
        if test_reconnect and report.reconnect_ok is None:
            report.reconnect_ok = False

    report.recommendation = _recommendation(report.ok, report.reconnect_ok)
    return report


def _write_report(path: Path, report: BinanceConnectivityReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Z-Gap A0.0.b Binance WS connectivity preflight")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument(
        "--streams",
        nargs="+",
        default=["bookTicker", "aggTrade"],
        help="Binance stream names (default: bookTicker aggTrade)",
    )
    parser.add_argument("--ws-base", default=DEFAULT_BINANCE_WS_BASE)
    parser.add_argument("--connect-timeout-s", type=float, default=15.0)
    parser.add_argument("--first-message-timeout-s", type=float, default=20.0)
    parser.add_argument("--no-reconnect-test", action="store_true")
    parser.add_argument(
        "--output",
        default="var/reporting/z_gap/binance_connectivity.json",
        help="JSON artifact path",
    )
    args = parser.parse_args(argv)

    report = asyncio.run(
        run_check(
            symbol=args.symbol,
            streams=tuple(args.streams),
            ws_base=args.ws_base,
            connect_timeout_s=args.connect_timeout_s,
            first_message_timeout_s=args.first_message_timeout_s,
            test_reconnect=not args.no_reconnect_test,
        )
    )

    out = Path(args.output)
    _write_report(out, report)

    print(json.dumps(asdict(report), indent=2))
    print(f"\nWrote {out}")
    if report.ok:
        print("RESULT: PASS")
        return 0
    print("RESULT: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
