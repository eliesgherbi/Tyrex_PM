"""Deduplicate, publish guru signals, advance watermark — shared poll + RTDS (C1)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from tyrex_pm.core.types import GuruTradeSignal
from tyrex_pm.data.guru_dedup import GuruDedupStore
from tyrex_pm.data.guru_watermark import GuruWatermarkStore

if TYPE_CHECKING:
    from nautilus_trader.common.component import MessageBus


LogFn = Callable[..., None]
FactEmitFn = Callable[[str, dict[str, Any]], None]


class GuruSignalPipeline:
    """Single-writer path for ``GuruTradeSignal`` emission + persistent dedup/watermark."""

    __slots__ = ("_msgbus", "_topic", "_log", "_dedup", "_watermark", "_emit_fact")

    def __init__(
        self,
        msgbus: MessageBus,
        topic: str,
        log: LogFn,
        dedup: GuruDedupStore,
        watermark: GuruWatermarkStore,
        emit_fact: Optional[FactEmitFn] = None,
    ) -> None:
        self._msgbus = msgbus
        self._topic = topic
        self._log = log
        self._dedup = dedup
        self._watermark = watermark
        self._emit_fact = emit_fact

    def try_publish(
        self,
        sig: GuruTradeSignal,
        *,
        source: str,
        ts_recv_ms: int | None = None,
        extra_kv: str = "",
    ) -> bool:
        if not self._dedup.is_new(sig.source_trade_id):
            return False
        self._dedup.remember(sig.source_trade_id)
        self._msgbus.publish(self._topic, sig)
        self._watermark.advance(sig.ts_event_ms)
        ts_emit = int(time.time() * 1000)
        recv_part = f" ts_recv_ms={ts_recv_ms}" if ts_recv_ms is not None else ""
        tok = sig.token_id or ""
        self._log(
            "event=guru_signal_emitted "
            f"component=guru_ingest source={source}{recv_part} ts_emit_ms={ts_emit} "
            f"correlation_id={sig.source_trade_id} side={sig.side} token_id={tok} "
            f"ts_event_ms={sig.ts_event_ms}{extra_kv}",
        )
        emit = self._emit_fact
        if emit is not None:
            dte = None
            if ts_recv_ms is not None:
                dte = ts_emit - int(sig.ts_event_ms)
            emit(
                "guru_signal",
                {
                    "correlation_id": sig.source_trade_id,
                    "source": source,
                    "side": sig.side,
                    "token_id": tok,
                    "ts_event_ms": int(sig.ts_event_ms),
                    "ts_emit_ms": ts_emit,
                    "ts_recv_ms": ts_recv_ms,
                    "guru_size_raw": sig.size_raw,
                    "guru_price_raw": sig.price_raw,
                    "detection_to_emit_ms": dte,
                },
            )
        return True
