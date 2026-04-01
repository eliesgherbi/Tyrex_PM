"""
Nautilus `Actor` that polls Polymarket Data API for a guru wallet and publishes `GuruTradeSignal`.

Rate limits: https://docs.polymarket.com/quickstart/introduction/rate-limits

No order placement — keep execution stack out of `data/`.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.events import TimeEvent
from nautilus_trader.config import ActorConfig

from tyrex_pm.core.types import GuruTradeSignal
from tyrex_pm.data.data_api_client import PolymarketDataApiClient
from tyrex_pm.data.guru_dedup import GuruDedupStore
from tyrex_pm.data.guru_parse import trade_row_to_signal


class GuruMonitorActorConfig(ActorConfig, frozen=True):
    """Configuration for `GuruMonitorActor`."""

    guru_wallet_address: str
    poll_interval_secs: float = 30.0
    data_api_base_url: str = "https://data-api.polymarket.com"
    dedup_state_path: str | None = None


GURU_TRADE_TOPIC = "tyrex_pm.guru.GuruTradeSignal"


class GuruMonitorActor(Actor):
    """
    Polls `GET /trades?user=<wallet>` on a timer, deduplicates, publishes on the message bus.

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

    def on_start(self) -> None:
        if self._client is None:
            self._client = PolymarketDataApiClient(
                self._cfg.data_api_base_url,
                log_backoff=self._backoff_log,
            )
        self._dedup.load()
        self.log.info("event=guru_poll_tick component=guru_monitor phase=on_start")
        self._poll_trades("on_start")
        self.clock.set_timer(
            name="guru_monitor_poll",
            interval=timedelta(seconds=self._cfg.poll_interval_secs),
            callback=self._handle_poll_tick,
        )

    def _handle_poll_tick(self, event: TimeEvent) -> None:
        _ = event
        self.log.info("event=guru_poll_tick component=guru_monitor phase=timer")
        self._poll_trades("timer")

    def _backoff_log(self, **kwargs: Any) -> None:
        tail = " ".join(f"{k}={kwargs[k]}" for k in sorted(kwargs))
        self.log.info(f"event=poller_backoff {tail}")

    def _poll_trades(self, phase: str) -> None:
        assert self._client is not None
        self.log.info(f"event=guru_poll_tick component=guru_monitor phase={phase} sub=fetch")
        offset = 0
        while True:
            rows = self._client.get_trades(
                user=self._cfg.guru_wallet_address,
                limit=100,
                offset=offset,
                taker_only=False,
            )
            if not rows:
                break
            for row in rows:
                self._ingest_row(row)
            offset += len(rows)
            if len(rows) < 100:
                break

    def _ingest_row(self, row: dict[str, Any]) -> None:
        sig = trade_row_to_signal(row)
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
