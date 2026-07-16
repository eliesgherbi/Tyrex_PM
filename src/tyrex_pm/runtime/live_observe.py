"""Live public read-only observe / dry-plan runner (R3B/R4 — no OMS)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from tyrex_pm.adapters.binance.ws_adapter import BinanceTradeWsAdapter
from tyrex_pm.adapters.polymarket.ws_adapter import PolymarketMarketWsAdapter
from tyrex_pm.core.clock import SystemClock
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.runtime.config import ObserveConfig, SourceMode
from tyrex_pm.runtime.observe_host import ObserveHost, ObserveRunResult, resolve_market_for_config


async def run_live_observe(config: ObserveConfig) -> ObserveRunResult:
    if config.mode is not SourceMode.LIVE:
        raise ValueError("run_live_observe requires live mode")
    if config.runtime_duration is None:
        raise ValueError("live mode requires runtime_duration")

    host = ObserveHost(config, clock=SystemClock())
    host._attach()
    host._init_flags()
    host._emit(
        "runtime_start",
        {
            "mode": config.mode.value,
            "runtime_mode": host._runtime_mode().value,
            "config_fingerprint": config.fingerprint(),
        },
    )

    market: BinaryMarket = await resolve_market_for_config(config)
    host.registry.set_market(market)
    host._start_strategy(market)
    host._emit(
        "market_resolved",
        {
            "market_id": market.market_id.value,
            "condition_id": market.condition_id,
            "yes_token": market.yes.token_id.value,
            "no_token": market.no.token_id.value,
            "question": market.question,
            "event_slug": market.event_slug,
        },
    )

    def on_pm_health(status: str, detail: dict) -> None:
        host._emit("adapter_health", {"adapter": "polymarket", "status": status, **detail})
        if status == "reconnecting":
            now = datetime.now(timezone.utc)
            host.book_store.invalidate(
                market.yes.instrument_id, ts_event=now, ts_received=now
            )
            host.book_store.invalidate(
                market.no.instrument_id, ts_event=now, ts_received=now
            )
            host._emit(
                "book_state_invalidated",
                {"reason": "polymarket_reconnect", "recovery_required": True},
            )

    def on_bn_health(status: str, detail: dict) -> None:
        host._emit("adapter_health", {"adapter": "binance", "status": status, **detail})

    pm = PolymarketMarketWsAdapter(
        asset_ids=[market.yes.token_id.value, market.no.token_id.value],
        market_id=market.market_id,
        correlation_id=host.correlation_id,
        on_health=on_pm_health,
    )
    bn = BinanceTradeWsAdapter(
        symbol=config.binance_symbol,
        correlation_id=host.correlation_id,
        on_health=on_bn_health,
        reset_indicator=host.momentum.reset,
    )

    tasks = [
        asyncio.create_task(pm.run(host.dispatcher), name="polymarket_ws"),
        asyncio.create_task(bn.run(host.dispatcher), name="binance_ws"),
    ]
    try:
        await asyncio.sleep(config.runtime_duration.total_seconds())
    finally:
        await pm.stop()
        await bn.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        host.strategy.on_stop("NORMAL")
        host._emit(
            "runtime_stop",
            {
                "decision_count": len(host.decisions),
                "signal_count": len(host.signals),
                "intent_count": len(host.intents),
                "risk_count": len(host.risk_decisions),
                "plan_count": len(host.plans),
            },
        )
        host.sink.flush()
        host.close()

    return ObserveRunResult(
        run_id=host.run_id,
        correlation_id=host.correlation_id,
        market=market,
        decisions=list(host.decisions),
        signals=list(host.signals),
        intents=list(host.intents),
        risk_decisions=list(host.risk_decisions),
        plans=list(host.plans),
        facts_path=host.sink.path,
        fact_count=host.sink.count,
    )
