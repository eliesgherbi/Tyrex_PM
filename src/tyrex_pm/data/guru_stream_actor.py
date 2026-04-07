"""
RTDS WebSocket ``activity`` / ``trades`` → ``GuruTradeSignal`` (Nautilus ``Actor``).

Drains a background WebSocket thread via a timer; optional shadow-only logging.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from queue import Empty, Queue
from typing import Any, Optional

from tyrex_pm.data.guru_ingest_pipeline import FactEmitFn
from nautilus_trader.common.actor import Actor
from nautilus_trader.common.events import TimeEvent
from nautilus_trader.config import ActorConfig

from tyrex_pm.data.data_api_client import PolymarketDataApiClient
from tyrex_pm.data.guru_dedup import GuruDedupStore
from tyrex_pm.data.guru_gap_fill import gap_fill_resilient
from tyrex_pm.data.guru_ingest_pipeline import GuruSignalPipeline
from tyrex_pm.data.guru_ingest_state import GuruIngestRuntimeState
from tyrex_pm.data.guru_monitor import GURU_TRADE_TOPIC
from tyrex_pm.data.guru_rtds_parse import (
    guru_proxy_wallet_from_payload,
    normalize_wallet,
    rtds_trade_payload_to_signal,
)
from tyrex_pm.data.guru_rtds_ws import RtdsActivityTradesWorker
from tyrex_pm.data.guru_watermark import GuruWatermarkStore


class GuruStreamActorConfig(ActorConfig, frozen=True):
    guru_wallet_address: str
    rtds_url: str = "wss://ws-live-data.polymarket.com"
    data_api_base_url: str = "https://data-api.polymarket.com"
    dedup_state_path: str | None = None
    watermark_state_path: str | None = None
    activity_limit: int = 200
    max_activity_pages_per_poll: int = 4
    rtds_ping_interval_seconds: float = 5.0
    rtds_liveness_timeout_seconds: float = 120.0
    rtds_reconnect_backoff_initial_seconds: float = 1.0
    rtds_reconnect_backoff_max_seconds: float = 60.0
    gap_fill_enabled: bool = True
    gap_fill_lookback_seconds: float = 60.0
    stream_queue_drain_interval_ms: int = 50


class GuruStreamActor(Actor):
    def __init__(
        self,
        config: GuruStreamActorConfig,
        *,
        dedup: GuruDedupStore,
        watermark: GuruWatermarkStore,
        ingest_state: GuruIngestRuntimeState,
        data_client: PolymarketDataApiClient | None = None,
        emit_fact: Optional[FactEmitFn] = None,
    ) -> None:
        super().__init__(config)
        self._cfg = config
        self._dedup = dedup
        self._watermark = watermark
        self._ingest_state = ingest_state
        self._http: PolymarketDataApiClient | None = data_client
        self._queue: Queue[Any] = Queue()
        self._stop_ws = threading.Event()
        self._worker: RtdsActivityTradesWorker | None = None
        self._pipeline: GuruSignalPipeline | None = None
        self._reconnect_count = 0
        self._stall_count = 0
        self._gap_fill_count = 0
        self._fallback_activation_count = 0
        self._emit_fact = emit_fact
        guru_w = normalize_wallet(config.guru_wallet_address)
        self.log.info(
            "event=guru_stream_start component=guru_stream "
            f"guru_wallet_norm={guru_w} rtds_url={config.rtds_url} ingest_mode={ingest_state.mode} "
            "rtds_match_field=proxyWallet",
        )

    def on_start(self) -> None:
        def _rtds_log(msg: str) -> None:
            self.log.info(f"{msg} component=guru_stream")

        self._pipeline = GuruSignalPipeline(
            self.msgbus,
            GURU_TRADE_TOPIC,
            self.log.info,
            self._dedup,
            self._watermark,
            emit_fact=self._emit_fact,
        )
        if self._http is None:
            self._http = PolymarketDataApiClient(self._cfg.data_api_base_url, log_backoff=None)

        self._worker = RtdsActivityTradesWorker(
            self._cfg.rtds_url,
            self._queue,
            self._stop_ws,
            ping_interval=self._cfg.rtds_ping_interval_seconds,
            liveness_timeout=self._cfg.rtds_liveness_timeout_seconds,
            reconnect_backoff_initial=self._cfg.rtds_reconnect_backoff_initial_seconds,
            reconnect_backoff_max=self._cfg.rtds_reconnect_backoff_max_seconds,
            subscribe_envelope=None,
            log=_rtds_log,
        )
        self._worker.start()

        self.clock.set_timer(
            name="guru_stream_drain",
            interval=timedelta(milliseconds=max(20, int(self._cfg.stream_queue_drain_interval_ms))),
            callback=self._handle_drain,
        )

    def on_dispose(self) -> None:
        self._stop_ws.set()
        if self._worker is not None:
            self._worker.stop_join(timeout=8.0)
            self._worker = None
        super().on_dispose()

    def _handle_drain(self, event: TimeEvent) -> None:
        _ = event
        assert self._pipeline is not None
        pipe = self._pipeline
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                break
            if item == "RECONNECT":
                self._reconnect_count += 1
                self.log.info(
                    "event=guru_rtds_reconnect component=guru_stream "
                    f"total={self._reconnect_count} gap_fill_enabled={self._cfg.gap_fill_enabled}",
                )
                ef = self._emit_fact
                if ef is not None:
                    ef(
                        "health_anomaly",
                        {
                            "component": "guru_stream",
                            "event_type": "guru_rtds_reconnect",
                            "detail": f"total={self._reconnect_count}",
                        },
                    )
                reason = self._ingest_state.activate_fallback_poll("rtds_reconnect")
                if reason:
                    self._fallback_activation_count += 1
                    self.log.warning(
                        "event=guru_ingest_fallback_activation component=guru_stream "
                        f"reason={reason} total={self._fallback_activation_count}",
                    )
                    if ef is not None:
                        ef(
                            "health_anomaly",
                            {
                                "component": "guru_stream",
                                "event_type": "guru_ingest_fallback_activation",
                                "detail": str(reason),
                            },
                        )
                if self._cfg.gap_fill_enabled and self._ingest_state.mode == "rtds_primary":
                    self._run_gap_fill()
                continue
            if item == "STALL":
                self._stall_count += 1
                self.log.warning(
                    f"event=guru_rtds_stall component=guru_stream total={self._stall_count}",
                )
                ef = self._emit_fact
                if ef is not None:
                    ef(
                        "health_anomaly",
                        {
                            "component": "guru_stream",
                            "event_type": "guru_rtds_stall",
                            "detail": f"total={self._stall_count}",
                        },
                    )
                reason = self._ingest_state.activate_fallback_poll("rtds_stall")
                if reason:
                    self._fallback_activation_count += 1
                    self.log.warning(
                        "event=guru_ingest_fallback_activation component=guru_stream "
                        f"reason={reason} total={self._fallback_activation_count}",
                    )
                    if ef is not None:
                        ef(
                            "health_anomaly",
                            {
                                "component": "guru_stream",
                                "event_type": "guru_ingest_fallback_activation",
                                "detail": str(reason),
                            },
                        )
                continue
            if not isinstance(item, dict):
                continue
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            if self._ingest_state.clear_fallback_poll():
                self.log.info(
                    "event=guru_ingest_fallback_cleared component=guru_stream reason=rtds_envelope",
                )
                ef = self._emit_fact
                if ef is not None:
                    ef(
                        "health_anomaly",
                        {
                            "component": "guru_stream",
                            "event_type": "guru_ingest_fallback_cleared",
                            "detail": "rtds_envelope",
                        },
                    )
            proxy = guru_proxy_wallet_from_payload(payload)
            proxy_n = normalize_wallet(proxy)
            guru_n = normalize_wallet(self._cfg.guru_wallet_address)
            if proxy_n != guru_n:
                continue
            sig = rtds_trade_payload_to_signal(payload)
            if sig is None:
                continue
            ts_recv = int(time.time() * 1000)
            if self._ingest_state.stream_should_publish():
                pipe.try_publish(
                    sig,
                    source="rtds",
                    ts_recv_ms=ts_recv,
                    extra_kv=f" detection_to_emit_ms={ts_recv - sig.ts_event_ms}",
                )
            elif self._ingest_state.stream_shadow_log_would_emit():
                would = self._dedup.is_new(sig.source_trade_id)
                self.log.info(
                    "event=guru_stream_would_emit component=guru_stream correlation_id={} "
                    "side={} token_id={} ts_event_ms={} ts_recv_ms={} would_publish_new={}".format(
                        sig.source_trade_id,
                        sig.side,
                        sig.token_id or "",
                        sig.ts_event_ms,
                        ts_recv,
                        would,
                    ),
                )
                ef = self._emit_fact
                if ef is not None:
                    ef(
                        "guru_shadow_compare",
                        {
                            "correlation_id": sig.source_trade_id,
                            "side": sig.side,
                            "token_id": sig.token_id or "",
                            "ts_event_ms": int(sig.ts_event_ms),
                            "ts_recv_ms": ts_recv,
                            "would_publish_new": bool(would),
                        },
                    )

    def _run_gap_fill(self) -> None:
        assert self._http is not None
        self._gap_fill_count += 1
        self.log.info(f"event=guru_gap_fill_begin component=guru_stream n={self._gap_fill_count}")
        gap_fill_resilient(
            client=self._http,
            guru_wallet=self._cfg.guru_wallet_address,
            watermark=self._watermark,
            dedup=self._dedup,
            activity_limit=self._cfg.activity_limit,
            max_pages=self._cfg.max_activity_pages_per_poll,
            lookback_seconds=self._cfg.gap_fill_lookback_seconds,
            topic=GURU_TRADE_TOPIC,
            msgbus=self.msgbus,
            log=self.log,
            component="guru_stream_gap_fill",
            emit_fact=self._emit_fact,
        )
