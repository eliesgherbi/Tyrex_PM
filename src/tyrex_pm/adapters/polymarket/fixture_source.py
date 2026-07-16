"""Deterministic Polymarket event publisher from fixture JSON."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tyrex_pm.adapters.polymarket.normalize import normalize_market_ws_message
from tyrex_pm.core.ids import CorrelationId, MarketId, new_correlation_id
from tyrex_pm.engine.dispatcher import EventDispatcher


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        ms = int(value)
        if ms < 10_000_000_000:
            ms *= 1000
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text).astimezone(timezone.utc)


class PolymarketFixtureSource:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._payload = json.loads(self._path.read_text(encoding="utf-8"))

    def publish_all(
        self,
        dispatcher: EventDispatcher,
        *,
        correlation_id: CorrelationId | None = None,
        market_id: MarketId | None = None,
    ) -> int:
        corr = correlation_id or new_correlation_id()
        count = 0
        for item in self._payload.get("polymarket_events", []):
            ts_received = _parse_ts(item.get("ts_received") or item.get("timestamp"))
            body = item.get("payload") or item
            event = normalize_market_ws_message(
                body,
                ts_received=ts_received,
                correlation_id=corr,
                market_id=market_id,
            )
            if event is None:
                continue
            dispatcher.publish(event)
            count += 1
        return count
