"""REST ``/activity`` gap-fill after RTDS reconnect."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Optional

import httpx

from tyrex_pm.core.types import GuruTradeSignal
from tyrex_pm.data.data_api_client import PolymarketDataApiClient
from tyrex_pm.data.guru_dedup import GuruDedupStore
from tyrex_pm.data.guru_parse import activity_trade_row_to_signal, api_timestamp_to_ms
from tyrex_pm.data.guru_watermark import GuruWatermarkStore


def run_activity_gap_fill(
    *,
    client: PolymarketDataApiClient,
    guru_wallet: str,
    watermark: GuruWatermarkStore,
    activity_limit: int,
    max_pages: int,
    lookback_seconds: float,
    publish: Callable[[GuruTradeSignal, int | None], bool],
    log: Any,
    component: str = "guru_gap_fill",
) -> tuple[int, int]:
    """
    Incremental poll from watermark (with optional lookback window).

    ``publish(sig, None)`` should run dedup+msgbus like :class:`GuruSignalPipeline`.

    Returns ``(rows_fetched, signals_published)``.
    """

    assert watermark.last_seen_ts_ms is not None
    watermark_before = watermark.last_seen_ts_ms
    lb_ms = int(max(0.0, lookback_seconds) * 1000)
    start_ms = max(0, watermark_before - lb_ms)
    start_sec = start_ms // 1000

    limit = max(1, min(500, int(activity_limit)))
    max_pages = max(1, int(max_pages))
    all_rows: list[dict[str, Any]] = []
    for page in range(max_pages):
        offset = page * limit
        rows = client.get_user_trade_activity(
            user=guru_wallet,
            limit=limit,
            offset=offset,
            start_ts_sec=start_sec,
            sort_direction="ASC",
        )
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < limit:
            break

    if not all_rows:
        log.info(f"event=guru_gap_fill component={component} rows=0 published=0 reason=empty")
        return (0, 0)

    max_ts_ms = watermark_before
    for row in all_rows:
        max_ts_ms = max(max_ts_ms, api_timestamp_to_ms(row.get("timestamp")))

    ordered = sorted(
        all_rows,
        key=lambda r: (
            api_timestamp_to_ms(r.get("timestamp")),
            str(r.get("transactionHash") or ""),
            str(r.get("asset") or ""),
        ),
    )
    ts_fill = int(time.time() * 1000)
    published = 0
    for row in ordered:
        if str(row.get("type") or "TRADE").upper() != "TRADE":
            continue
        ts_ms = api_timestamp_to_ms(row.get("timestamp"))
        if ts_ms <= watermark_before:
            continue
        sig = activity_trade_row_to_signal(row)
        if publish(sig, ts_fill):
            published += 1

    watermark.advance(max_ts_ms)
    log.info(
        f"event=guru_gap_fill component={component} rows={len(all_rows)} "
        f"published={published} ts_fill_ms={ts_fill}",
    )
    return (len(all_rows), published)


def gap_fill_resilient(
    *,
    client: PolymarketDataApiClient,
    guru_wallet: str,
    watermark: GuruWatermarkStore,
    dedup: GuruDedupStore,
    activity_limit: int,
    max_pages: int,
    lookback_seconds: float,
    topic: str,
    msgbus: Any,
    log: Any,
    component: str = "guru_gap_fill",
    emit_fact: Optional[Callable[[str, dict[str, Any]], None]] = None,
) -> tuple[int, int]:
    """Gap-fill using :class:`~tyrex_pm.data.guru_ingest_pipeline.GuruSignalPipeline`-compatible publish."""

    from tyrex_pm.data.guru_ingest_pipeline import GuruSignalPipeline

    pipe = GuruSignalPipeline(msgbus, topic, log.info, dedup, watermark, emit_fact=emit_fact)

    def _pub(sig: GuruTradeSignal, ts_fill: int | None) -> bool:
        return pipe.try_publish(sig, source="gap_fill", ts_recv_ms=ts_fill)

    try:
        return run_activity_gap_fill(
            client=client,
            guru_wallet=guru_wallet,
            watermark=watermark,
            activity_limit=activity_limit,
            max_pages=max_pages,
            lookback_seconds=lookback_seconds,
            publish=_pub,
            log=log,
            component=component,
        )
    except (httpx.HTTPStatusError, httpx.RequestError, OSError, ValueError) as exc:
        log.error(
            f"event=guru_gap_fill_error component={component} err_type={type(exc).__name__} err={exc}",
        )
        return (0, 0)
