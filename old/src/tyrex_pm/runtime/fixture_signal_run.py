"""Generic fixture/signal runner for non-guru CLI harnesses (P1 runtime glue).

Provides a single-shot path:

```text
config signal → Signal → Strategy.on_signal → process_signals → pipeline
```

No guru polling, no Data API, no ``guru.wallet``. ``simple_signal_test`` is the
first consumer; future non-guru harnesses can reuse the same runner.
"""

from __future__ import annotations

import asyncio
import logging
import os
from decimal import Decimal

from tyrex_pm.core.enums import ExecutionMode, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_HEALTH
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import SELL_TEST_PRICING_AUTO, SimpleSignalTestStrategyConfig
from tyrex_pm.runtime.pipeline import process_signals
from tyrex_pm.signals.simple_signal import SIMPLE_SIGNAL_TYPE_ENTRY, SimpleSignal
from tyrex_pm.strategies.sell_test.pricing import resolve_marketable_price_via_client
from tyrex_pm.strategies.simple_signal_test.strategy import SimpleSignalTestStrategy
from tyrex_pm.core.time import monotonic_s

log = logging.getLogger(__name__)


def build_simple_signal_from_config(
    cfg: SimpleSignalTestStrategyConfig,
    *,
    dedup_key: str | None = None,
    limit_price: Decimal | None = None,
) -> SimpleSignal:
    """Build a :class:`SimpleSignal` from a loaded ``simple_signal_test`` config."""
    effective_price = limit_price if limit_price is not None else cfg.limit_price
    return SimpleSignal(
        token_id=TokenId(cfg.token_id),
        side=cfg.side,
        order_style=cfg.order_style,
        dedup_key=dedup_key or f"simple_signal_test:{cfg.token_id}",
        notional_usd=cfg.notional_usd,
        limit_price=effective_price,
        signal_type=SIMPLE_SIGNAL_TYPE_ENTRY,
    )


async def _resolve_live_limit_price(
    cfg: SimpleSignalTestStrategyConfig,
    *,
    coord,
    live_clob_client: object,
    sink: JsonlSink,
    run_id: RunId,
) -> Decimal | None:
    """Resolve a marketable BUY limit for live auto-pricing (mirrors sell_test)."""
    if cfg.side != Side.BUY or cfg.pricing_mode != SELL_TEST_PRICING_AUTO:
        return cfg.limit_price
    market_info = None
    if coord.market_info_cache is not None:
        try:
            market_info = await coord.market_info_cache.get(cfg.token_id)
        except Exception:  # noqa: BLE001 — pricing falls back regardless
            market_info = None
    resolved = await resolve_marketable_price_via_client(
        client=live_clob_client,
        market_info=market_info,
        token_id=cfg.token_id,
        side="BUY",
        aggression_ticks=cfg.aggression_ticks,
        fallback_price=cfg.limit_price,
        max_price=cfg.max_price,
    )
    sink.write(
        make_fact(
            FACT_TYPE_HEALTH,
            str(run_id),
            {
                "event": "simple_signal_test_pricing",
                "pricing_mode": cfg.pricing_mode,
                **resolved.to_evidence(),
            },
        )
    )
    if resolved.source == "fallback" and resolved.error:
        log.warning(
            "simple_signal_test auto pricing failed (%s); using fallback limit_price=%s",
            resolved.error,
            cfg.limit_price,
        )
        return cfg.limit_price
    return resolved.price


async def _wait_live_allocation(
    coord,
    *,
    owner_id: str,
    token_id: str,
    timeout_s: float,
) -> bool:
    """Wait until allocation ledger shows bought inventory (live fill evidence)."""
    ledger = coord.allocation_ledger
    if ledger is None:
        return False
    deadline = monotonic_s() + timeout_s
    tid = TokenId(token_id)
    while monotonic_s() < deadline:
        if ledger.get_available_allocated(owner_id, tid) > 0:
            return True
        await asyncio.sleep(0.5)
    return False


async def run_fixture_signals_once(
    *,
    app,
    run_id: RunId,
    coord,
    sink,
    oms,
    cfg: SimpleSignalTestStrategyConfig,
    apply_local_shadow_fill: bool = True,
    live_clob_client: object | None = None,
) -> int:
    """Run one configured signal through :func:`process_signals` and return iteration count.

    Returns ``0`` when the harness is disabled; ``1`` after a successful dispatch.
    In live mode, optionally waits for allocation credit after submit so operators
    can confirm the BUY established before the process exits.
    """
    if not cfg.enabled:
        return 0

    limit_price = cfg.limit_price
    if (
        app.runtime.execution_mode == ExecutionMode.LIVE
        and live_clob_client is not None
        and cfg.pricing_mode == SELL_TEST_PRICING_AUTO
    ):
        limit_price = await _resolve_live_limit_price(
            cfg,
            coord=coord,
            live_clob_client=live_clob_client,
            sink=sink,
            run_id=run_id,
        )

    signal = build_simple_signal_from_config(cfg, limit_price=limit_price)
    strategy = SimpleSignalTestStrategy(owner_id=cfg.owner_id)
    await process_signals(
        [signal],
        app=app,
        run_id=run_id,
        strategy=strategy,
        coord=coord,
        sink=sink,
        oms=oms,
        apply_local_shadow_fill=apply_local_shadow_fill,
        live_clob_client=live_clob_client,
    )

    if app.runtime.execution_mode == ExecutionMode.LIVE and cfg.side == Side.BUY:
        fill_wait_s = float(os.environ.get("TYREX_SIMPLE_SIGNAL_TEST_FILL_WAIT_S", "45"))
        if fill_wait_s > 0:
            filled = await _wait_live_allocation(
                coord,
                owner_id=cfg.owner_id,
                token_id=cfg.token_id,
                timeout_s=fill_wait_s,
            )
            sink.write(
                make_fact(
                    FACT_TYPE_HEALTH,
                    str(run_id),
                    {
                        "event": "simple_signal_test_fill_wait",
                        "filled": filled,
                        "timeout_s": fill_wait_s,
                        "owner_id": cfg.owner_id,
                        "token_id": cfg.token_id,
                    },
                )
            )
            if filled:
                log.info(
                    "simple_signal_test live BUY established: owner=%s token=%s",
                    cfg.owner_id,
                    cfg.token_id,
                )
            else:
                log.warning(
                    "simple_signal_test live fill wait timed out after %.1fs "
                    "(order may still be resting on the book)",
                    fill_wait_s,
                )

    return 1
