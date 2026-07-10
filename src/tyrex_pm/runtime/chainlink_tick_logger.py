"""Minimal Chainlink tick logger sidecar for A0.5 PTB capture."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tyrex_pm.core.events import MarketEvent
from tyrex_pm.ingestion.reference_prices import run_reference_prices_ingest

log = logging.getLogger(__name__)

DEFAULT_CHAINLINK_TICKS_PATH = Path("var/state/chainlink_ticks.jsonl")
DEFAULT_RETENTION_S = 7200.0
DEFAULT_FEEDS = ("chainlink",)
DEFAULT_SYMBOLS = ("btc/usd",)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_ts(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def append_chainlink_tick_row(
    *,
    path: Path,
    source_ts: datetime,
    recv_ts: datetime,
    price: str,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "source_ts": source_ts.isoformat(),
        "recv_ts": recv_ts.isoformat(),
        "price": price,
    }
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def prune_chainlink_ticks(*, path: Path, retention_s: float = DEFAULT_RETENTION_S) -> int:
    """Drop rows older than retention window. Returns rows kept."""
    if not path.is_file():
        return 0
    cutoff = _utc_now().timestamp() - retention_s
    kept: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                continue
            source_raw = row.get("source_ts")
            if not source_raw:
                continue
            source_dt = _parse_iso_ts(str(source_raw))
            if source_dt is None:
                continue
            if source_dt.timestamp() >= cutoff:
                kept.append(text)
    path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return len(kept)


def load_chainlink_ticks(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or DEFAULT_CHAINLINK_TICKS_PATH
    if not target.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as fh:
        for line in fh:
            text = line.strip()
            if not text:
                continue
            try:
                rows.append(json.loads(text))
            except json.JSONDecodeError:
                continue
    return rows


@dataclass
class ChainlinkTickLoggerConfig:
    path: Path = DEFAULT_CHAINLINK_TICKS_PATH
    retention_s: float = DEFAULT_RETENTION_S
    feeds: tuple[str, ...] = DEFAULT_FEEDS
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    venue: str = "polymarket_rtds"
    reconnect_backoff_s: float = 3.0
    prune_interval_s: float = 300.0


async def run_chainlink_tick_logger(
    *,
    stop: asyncio.Event,
    config: ChainlinkTickLoggerConfig | None = None,
) -> None:
    cfg = config or ChainlinkTickLoggerConfig()

    async def _on_event(event: MarketEvent) -> None:
        payload = event.payload or {}
        if str(payload.get("feed") or "").lower() != "chainlink":
            return
        price = str(payload.get("value") or "")
        if not price:
            return
        source_ts = event.source_ts or event.recv_ts or _utc_now()
        recv_ts = event.recv_ts or _utc_now()
        append_chainlink_tick_row(
            path=cfg.path,
            source_ts=source_ts,
            recv_ts=recv_ts,
            price=price,
        )

    async def _prune_loop() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.prune_interval_s)
                break
            except asyncio.TimeoutError:
                prune_chainlink_ticks(path=cfg.path, retention_s=cfg.retention_s)

    prune_task = asyncio.create_task(_prune_loop())
    try:
        await run_reference_prices_ingest(
            feeds=list(cfg.feeds),
            symbols=list(cfg.symbols),
            venue=cfg.venue,
            stop=stop,
            on_event_emitted=_on_event,
            reconnect_backoff_s=cfg.reconnect_backoff_s,
        )
    finally:
        prune_task.cancel()
        try:
            await prune_task
        except asyncio.CancelledError:
            pass
