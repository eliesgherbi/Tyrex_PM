"""
Nautilus `Actor` that polls Polymarket Data API for **recent** guru TRADE activity.

Uses ``GET /activity`` (``type=TRADE``), a persistent **timestamp watermark**, and
optional dedup as a safety net — **not** full historical ``/trades`` pagination.

Rate limits: https://docs.polymarket.com/quickstart/introduction/rate-limits

No order placement — keep execution stack out of `data/`.
"""

from __future__ import annotations

import random
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from nautilus_trader.common.actor import Actor
from nautilus_trader.common.events import TimeEvent
from nautilus_trader.config import ActorConfig

from tyrex_pm.core.types import GuruTradeSignal
from tyrex_pm.data.data_api_client import PolymarketDataApiClient
from tyrex_pm.data.guru_dedup import GuruDedupStore
from tyrex_pm.data.guru_parse import activity_trade_row_to_signal, api_timestamp_to_ms
from tyrex_pm.data.guru_watermark import GuruWatermarkStore


class GuruMonitorActorConfig(ActorConfig, frozen=True):
    """Configuration for `GuruMonitorActor`."""

    guru_wallet_address: str
    poll_interval_secs: float = 30.0
    data_api_base_url: str = "https://data-api.polymarket.com"
    dedup_state_path: str | None = None
    watermark_state_path: str | None = None
    activity_limit: int = 200
    max_activity_pages_per_poll: int = 4
    startup_backfill_seconds: float = 0.0


GURU_TRADE_TOPIC = "tyrex_pm.guru.GuruTradeSignal"


class GuruMonitorActor(Actor):
    """
    Polls ``/activity`` (TRADE only) incrementally, deduplicates, publishes on the bus.

    Injected `PolymarketDataApiClient` is supported for tests.
    """

    def __init__(
        self,
        config: GuruMonitorActorConfig,
        *,
        data_client: PolymarketDataApiClient | None = None,
    ) -> None:
        super().__init__(config)
        self._cfg = config
        self._client: PolymarketDataApiClient | None = data_client
        dedup_path = Path(config.dedup_state_path) if config.dedup_state_path else None
        self._dedup = GuruDedupStore(dedup_path)
        wm_path = Path(config.watermark_state_path) if config.watermark_state_path else None
        self._watermark = GuruWatermarkStore(wm_path)
        self._poll_errors = 0

    def on_start(self) -> None:
        if self._client is None:
            self._client = PolymarketDataApiClient(
                self._cfg.data_api_base_url,
                log_backoff=self._backoff_log,
            )
        self._dedup.load()
        self._watermark.load()
        self._watermark.ensure_initialized(backfill_seconds=self._cfg.startup_backfill_seconds)
        self._watermark.persist()
        self.log.info("event=guru_poll_tick component=guru_monitor phase=on_start")
        self._poll_trades_resilient("on_start")
        self.clock.set_timer(
            name="guru_monitor_poll",
            interval=timedelta(seconds=self._cfg.poll_interval_secs),
            callback=self._handle_poll_tick,
        )

    def _handle_poll_tick(self, event: TimeEvent) -> None:
        _ = event
        self.log.info("event=guru_poll_tick component=guru_monitor phase=timer")
        self._poll_trades_resilient("timer")

    def _backoff_log(self, **kwargs: Any) -> None:
        tail = " ".join(f"{k}={kwargs[k]}" for k in sorted(kwargs))
        self.log.info(f"event=poller_backoff {tail}")

    def _poll_trades_resilient(self, phase: str) -> None:
        try:
            self._poll_incremental(phase)
            self._poll_errors = 0
        except (httpx.HTTPStatusError, httpx.RequestError, OSError, ValueError) as exc:
            self._poll_errors += 1
            self.log.error(
                "event=guru_poll_error "
                f"component=guru_monitor phase={phase} err_type={type(exc).__name__} err={exc}"
            )
            self._error_backoff_sleep()

    def _error_backoff_sleep(self) -> None:
        attempt = min(self._poll_errors, 8)
        delay = min(120.0, (2**attempt) + random.random())
        self.log.info(
            "event=guru_poll_error_backoff "
            f"component=guru_monitor sleep_s={delay:.2f} errors={self._poll_errors}"
        )
        time.sleep(delay)

    def _poll_incremental(self, phase: str) -> None:
        assert self._client is not None
        assert self._watermark.last_seen_ts_ms is not None
        self.log.info(f"event=guru_poll_tick component=guru_monitor phase={phase} sub=fetch")

        limit = max(1, min(500, int(self._cfg.activity_limit)))
        max_pages = max(1, int(self._cfg.max_activity_pages_per_poll))
        watermark_before = self._watermark.last_seen_ts_ms
        start_sec = watermark_before // 1000

        all_rows: list[dict[str, Any]] = []
        for page in range(max_pages):
            offset = page * limit
            rows = self._client.get_user_trade_activity(
                user=self._cfg.guru_wallet_address,
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
            return

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

        for row in ordered:
            if str(row.get("type") or "TRADE").upper() != "TRADE":
                continue
            ts_ms = api_timestamp_to_ms(row.get("timestamp"))
            if ts_ms <= watermark_before:
                continue
            sig = activity_trade_row_to_signal(row)
            self._ingest_row(sig)

        self._watermark.advance(max_ts_ms)

    def _ingest_row(self, sig: GuruTradeSignal) -> None:
        if not self._dedup.is_new(sig.source_trade_id):
            return
        self._dedup.remember(sig.source_trade_id)
        self._publish_signal(sig)
        tok = sig.token_id or ""
        self.log.info(
            "event=guru_signal_emitted "
            f"correlation_id={sig.source_trade_id} side={sig.side} token_id={tok}"
        )

    def _publish_signal(self, sig: GuruTradeSignal) -> None:
        self.msgbus.publish(GURU_TRADE_TOPIC, sig)
