"""Live public-data shadow runner (R5 — ShadowOMS only; no private/trading APIs)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from tyrex_pm.adapters.binance.ws_adapter import BinanceTradeWsAdapter
from tyrex_pm.adapters.polymarket.ws_adapter import PolymarketMarketWsAdapter
from tyrex_pm.core.clock import SystemClock
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.runtime.config import ObserveConfig, SourceMode
from tyrex_pm.runtime.observe_host import ObserveRunResult, resolve_market_for_config
from tyrex_pm.runtime.shadow_host import ShadowHost


async def run_live_shadow(config: ObserveConfig) -> ObserveRunResult:
    if config.mode is not SourceMode.LIVE:
        raise ValueError("run_live_shadow requires live mode")
    if config.runtime_duration is None:
        raise ValueError("live mode requires runtime_duration")
    if config.shadow is None or not config.shadow.enable_oms:
        raise ValueError("run_live_shadow requires shadow.enable_oms=true")

    host = ShadowHost(config, clock=SystemClock())
    host._attach()
    host._init_flags()
    host._emit(
        "runtime_start",
        {
            "mode": config.mode.value,
            "runtime_mode": host._runtime_mode().value,
            "shadow_oms": True,
            "fee_model": config.shadow.fee_model_id,
            "shadow_limitations": [
                "no_queue_position",
                "no_latency_model",
                "no_market_impact",
                "visible_depth_only",
                "not_profitability_evidence",
            ],
            "config_fingerprint": config.fingerprint(),
        },
    )

    market: BinaryMarket = await resolve_market_for_config(config)
    host.registry.set_market(market)
    host.portfolio.set_market_id(market.market_id)
    host._start_strategy(market)
    from tyrex_pm.persistence.snapshot import PersistenceError

    try:
        host.try_recover()
    except PersistenceError as exc:
        host._emit("recovery_skipped", {"reason": str(exc)})

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
            host.invalidate_shadow_books(reason="polymarket_reconnect")
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
        # Safe terminal policy: only FLAT (or already TERMINAL) may enter TERMINAL.
        # Residual shadow exposure is recorded; it is not venue inventory.
        now = datetime.now(timezone.utc)
        if host.lifecycle.state.value == "FLAT":
            host.lifecycle.mark_terminal(when=now)
        elif host.lifecycle.state.value != "TERMINAL":
            host._emit(
                "terminal_deferred_residual_exposure",
                {
                    "lifecycle": host.lifecycle.state.value,
                    "flat": host.portfolio.is_flat(),
                    "note": "ACTIVE/PENDING not forced to TERMINAL without flatten",
                },
            )
        host._maybe_persist()
        host._emit(
            "runtime_stop",
            {
                "decision_count": len(host.decisions),
                "signal_count": len(host.signals),
                "intent_count": len(host.intents),
                "command_count": len(host.commands),
                "risk_count": len(host.risk_decisions),
                "plan_count": len(host.plans),
                "lifecycle": host.lifecycle.state.value,
                "flat": host.portfolio.is_flat(),
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
