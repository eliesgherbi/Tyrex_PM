#!/usr/bin/env python3
"""N1 read-only multi-source capture for BTC 5m windows.

Public data only. No auth, wallets, orders, or old/ imports.
Outputs JSONL under var/reporting/n1/.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import websockets

RTDS_URL = "wss://ws-live-data.polymarket.com"
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
UA = "TyrexPM-N1-Audit/1.0 (read-only research)"
WINDOW_S = 300


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).isoformat()


def window_id_for(ts: float) -> str:
    epoch = int(ts) // WINDOW_S * WINDOW_S
    return f"btc-updown-5m-{epoch}"


def http_json(url: str) -> Any:
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fingerprint(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


class CaptureSink:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = 0
        self._fp = path.open("a", encoding="utf-8")
        self._mono0 = time.perf_counter_ns()

    def close(self) -> None:
        self._fp.close()

    def emit(self, *, source: str, symbol: str, value: Any, source_ts_ms: int | None,
             window_id: str | None = None, provider_seq: Any = None,
             connection_id: str = "", late_or_ooo: str | None = None,
             extra: dict[str, Any] | None = None) -> None:
        self._seq += 1
        wall = utc_now()
        mono = time.perf_counter_ns() - self._mono0
        src_ts = None
        if source_ts_ms is not None:
            src_ts = datetime.fromtimestamp(source_ts_ms / 1000.0, tz=timezone.utc)
        wid = window_id or (window_id_for(src_ts.timestamp()) if src_ts else window_id_for(wall.timestamp()))
        rec = {
            "capture_sequence": self._seq,
            "source": source,
            "symbol": symbol,
            "window_id": wid,
            "source_ts": iso(src_ts),
            "source_ts_ms": source_ts_ms,
            "receive_wall_utc": iso(wall),
            "receive_monotonic_ns": mono,
            "clock_uncertainty_ms": None,  # OS sync not measured in this audit script
            "value": value,
            "provider_sequence_id": provider_seq,
            "connection_id": connection_id,
            "late_or_out_of_order": late_or_ooo,
            "raw_event_fingerprint": fingerprint({"s": source, "sym": symbol, "v": value, "t": source_ts_ms}),
        }
        if extra:
            rec["extra"] = extra
        self._fp.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self._fp.flush()


async def rtds_loop(sink: CaptureSink, stop: asyncio.Event) -> None:
    conn_id = f"rtds-{int(time.time())}"
    last_cl: int | None = None
    last_bn: int | None = None
    while not stop.is_set():
        try:
            async with websockets.connect(RTDS_URL, ping_interval=None, close_timeout=5) as ws:
                # Empty Chainlink filters return all symbols (incl. btc/usd).
                # Symbol-filtered JSON is documented but observed less reliable here.
                sub = {
                    "action": "subscribe",
                    "subscriptions": [
                        {
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": "",
                        },
                        # Documented symbol filter \"btcusdt\" returned 0 msgs in N1;
                        # unfiltered stream includes btcusdt — client-filter below.
                        {
                            "topic": "crypto_prices",
                            "type": "update",
                        },
                    ],
                }
                await ws.send(json.dumps(sub))
                sink.emit(
                    source="rtds_control",
                    symbol="-",
                    value="subscribed",
                    source_ts_ms=int(time.time() * 1000),
                    connection_id=conn_id,
                    extra={"event": "subscribe"},
                )

                async def ping() -> None:
                    while not stop.is_set():
                        try:
                            await ws.send("PING")
                        except Exception:
                            return
                        await asyncio.sleep(5)

                ping_task = asyncio.create_task(ping())
                try:
                    while not stop.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        if msg == "PONG" or not msg or msg == "PING":
                            continue
                        try:
                            data = json.loads(msg)
                        except json.JSONDecodeError:
                            continue
                        topic = data.get("topic")
                        payload = data.get("payload") or {}
                        sym = str(payload.get("symbol") or "")
                        val = payload.get("value")
                        ts = payload.get("timestamp")
                        if ts is not None:
                            ts = int(ts)
                        if topic == "crypto_prices_chainlink" and "btc" in sym.lower():
                            ooo = None
                            if last_cl is not None and ts is not None and ts < last_cl:
                                ooo = "out_of_order"
                            if ts is not None:
                                last_cl = ts if last_cl is None else max(last_cl, ts)
                            sink.emit(
                                source="rtds_chainlink",
                                symbol=sym or "btc/usd",
                                value=val,
                                source_ts_ms=ts,
                                provider_seq=data.get("timestamp"),
                                connection_id=conn_id,
                                late_or_ooo=ooo,
                            )
                        elif topic == "crypto_prices" and "btc" in sym.lower():
                            ooo = None
                            if last_bn is not None and ts is not None and ts < last_bn:
                                ooo = "out_of_order"
                            if ts is not None:
                                last_bn = ts if last_bn is None else max(last_bn, ts)
                            sink.emit(
                                source="rtds_binance",
                                symbol=sym or "btcusdt",
                                value=val,
                                source_ts_ms=ts,
                                provider_seq=data.get("timestamp"),
                                connection_id=conn_id,
                                late_or_ooo=ooo,
                            )
                finally:
                    ping_task.cancel()
        except Exception as exc:
            sink.emit(
                source="rtds_control",
                symbol="-",
                value=f"reconnect:{type(exc).__name__}:{exc}",
                source_ts_ms=int(time.time() * 1000),
                connection_id=conn_id,
                extra={"event": "error"},
            )
            await asyncio.sleep(2)
            conn_id = f"rtds-{int(time.time())}"


async def binance_loop(sink: CaptureSink, stop: asyncio.Event) -> None:
    conn_id = f"binance-{int(time.time())}"
    last_ts: int | None = None
    while not stop.is_set():
        try:
            async with websockets.connect(BINANCE_WS, ping_interval=20, close_timeout=5) as ws:
                sink.emit(
                    source="binance_control",
                    symbol="BTCUSDT",
                    value="connected",
                    source_ts_ms=int(time.time() * 1000),
                    connection_id=conn_id,
                    extra={"event": "connect"},
                )
                while not stop.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    data = json.loads(msg)
                    # trade stream: E event time, T trade time, p price
                    ts = int(data.get("T") or data.get("E") or 0) or None
                    price = data.get("p")
                    ooo = None
                    if last_ts is not None and ts is not None and ts < last_ts:
                        ooo = "out_of_order"
                    if ts is not None:
                        last_ts = ts if last_ts is None else max(last_ts, ts)
                    sink.emit(
                        source="binance_spot_trade",
                        symbol="BTCUSDT",
                        value=float(price) if price is not None else None,
                        source_ts_ms=ts,
                        provider_seq=data.get("t"),
                        connection_id=conn_id,
                        late_or_ooo=ooo,
                    )
        except Exception as exc:
            sink.emit(
                source="binance_control",
                symbol="BTCUSDT",
                value=f"reconnect:{type(exc).__name__}:{exc}",
                source_ts_ms=int(time.time() * 1000),
                connection_id=conn_id,
                extra={"event": "error"},
            )
            await asyncio.sleep(2)
            conn_id = f"binance-{int(time.time())}"


async def ptb_poll_loop(sink: CaptureSink, stop: asyncio.Event, interval_s: float = 2.0) -> None:
    """Poll Gamma + known PTB URL candidates for displayed/structured PTB evidence."""
    last_seen: dict[str, Any] = {}
    while not stop.is_set():
        now = time.time()
        epochs = [(int(now) // WINDOW_S) * WINDOW_S + off for off in (-WINDOW_S, 0, WINDOW_S)]
        for epoch in epochs:
            slug = f"btc-updown-5m-{epoch}"
            # Gamma market payload (no dedicated crypto PTB field historically)
            try:
                events = http_json(f"{GAMMA_EVENTS}?slug={slug}")
                if events:
                    m = (events[0].get("markets") or [{}])[0]
                    sink.emit(
                        source="gamma_market_poll",
                        symbol=slug,
                        value={
                            "market_id": m.get("id"),
                            "conditionId": m.get("conditionId"),
                            "outcomes": m.get("outcomes"),
                            "eventStartTime": m.get("eventStartTime"),
                            "endDate": m.get("endDate"),
                            "resolutionSource": m.get("resolutionSource"),
                        },
                        source_ts_ms=int(now * 1000),
                        window_id=slug,
                        extra={"event": "gamma_poll"},
                    )
            except Exception as exc:
                sink.emit(
                    source="gamma_market_poll",
                    symbol=slug,
                    value=f"error:{type(exc).__name__}",
                    source_ts_ms=int(now * 1000),
                    window_id=slug,
                )

            for path in (
                f"https://polymarket.com/api/crypto/price-to-beat/{slug}",
                f"https://polymarket.com/api/price-to-beat/{slug}",
                f"https://gamma-api.polymarket.com/crypto/price-to-beat/{slug}",
            ):
                try:
                    data = http_json(path)
                    key = f"{path}:{slug}"
                    if last_seen.get(key) != data:
                        last_seen[key] = data
                        sink.emit(
                            source="ptb_http",
                            symbol=slug,
                            value=data,
                            source_ts_ms=int(now * 1000),
                            window_id=slug,
                            extra={"url": path},
                        )
                except Exception:
                    pass
        await asyncio.sleep(interval_s)


async def run(duration_s: float, out: Path) -> None:
    sink = CaptureSink(out)
    stop = asyncio.Event()
    sink.emit(
        source="capture_control",
        symbol="-",
        value="start",
        source_ts_ms=int(time.time() * 1000),
        extra={"duration_s": duration_s, "out": str(out)},
    )
    tasks = [
        asyncio.create_task(rtds_loop(sink, stop)),
        asyncio.create_task(binance_loop(sink, stop)),
        asyncio.create_task(ptb_poll_loop(sink, stop)),
    ]
    try:
        await asyncio.sleep(duration_s)
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        sink.emit(
            source="capture_control",
            symbol="-",
            value="stop",
            source_ts_ms=int(time.time() * 1000),
        )
        sink.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--duration-s", type=float, default=960.0, help="capture duration (default ~16m for 3 windows)")
    p.add_argument(
        "--out",
        type=Path,
        default=Path("var/reporting/n1/raw_capture.jsonl"),
    )
    args = p.parse_args()
    asyncio.run(run(args.duration_s, args.out))


if __name__ == "__main__":
    main()
