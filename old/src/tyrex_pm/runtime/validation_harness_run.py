"""Validation harness runner (P4.5 architecture_enhance).

Orchestrates operator validation modes through the generic pipeline without guru
polling. See ``Docs/Implementation/architecture_enhance/phase_4_5_live_validation_harness.md``.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from tyrex_pm.core.enums import ExecutionMode, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import URGENCY_URGENT, WalletPosition
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_HEALTH
from tyrex_pm.runtime.config import (
    VALIDATION_MODE_MARKET_DATA_READONLY,
    VALIDATION_MODE_NORMAL_ENTRY,
    VALIDATION_MODE_PROTECTION_REGISTER,
    VALIDATION_MODE_PROTECTION_SL,
    VALIDATION_MODE_PROTECTION_TP,
    VALIDATION_MODE_PROTECTION_TRIGGER_LIVE,
    VALIDATION_MODE_STALE_BOOK_DENY,
    VALIDATION_MODE_URGENT_EXIT,
    ValidationHarnessStrategyConfig,
)
from tyrex_pm.runtime.finality_waiter import wait_for_allocation_final
from tyrex_pm.runtime.market_data_runtime import (
    bootstrap_market_state,
    ensure_market_state_store,
    inject_fixture_book,
    run_market_data_readonly,
)
from tyrex_pm.runtime.pipeline import process_signals
from tyrex_pm.runtime.protection_runtime import (
    init_protection_monitor,
    register_protection_after_finality,
    register_protection_from_venue_position,
    register_protection_manual,
    run_protection_tick,
)
from tyrex_pm.runtime.protection_supervisor import run_protection_supervisor
from tyrex_pm.runtime.validation_harness_live import (
    emit_preflight,
    may_seed_allocation,
    may_use_fixture_book,
    preflight_urgent_exit,
    validate_validation_harness_live_config,
)
from tyrex_pm.signals.validation_signal import ValidationSignal
from tyrex_pm.strategies.sell_test.pricing import resolve_marketable_price_via_client
from tyrex_pm.strategies.validation_harness.strategy import ValidationHarnessStrategy

log = logging.getLogger(__name__)


def build_validation_signal_from_config(
    cfg: ValidationHarnessStrategyConfig,
    *,
    mode: str | None = None,
    side: Side | None = None,
    limit_price: Decimal | None = None,
    urgency: str | None = None,
    dedup_key: str | None = None,
    size: Decimal | None = None,
) -> ValidationSignal:
    effective_mode = mode or cfg.mode
    effective_side = side or cfg.side
    return ValidationSignal(
        token_id=TokenId(cfg.token_id),
        side=effective_side,
        order_style=cfg.order_style,
        dedup_key=dedup_key or f"validation_harness:{effective_mode}:{cfg.token_id}",
        mode=effective_mode,
        size=size if size is not None else cfg.size,
        notional_usd=cfg.notional_usd,
        limit_price=limit_price if limit_price is not None else cfg.limit_price,
        urgency=urgency or cfg.urgency,
    )


async def _resolve_entry_limit_price(
    cfg: ValidationHarnessStrategyConfig,
    *,
    app,
    coord,
    live_clob_client: object | None,
    sink,
    run_id: RunId,
) -> Decimal | None:
    if (
        app.runtime.execution_mode == ExecutionMode.LIVE
        and live_clob_client is not None
        and cfg.pricing_mode == "auto"
        and cfg.side == Side.BUY
    ):
        from tyrex_pm.runtime.config import SELL_TEST_PRICING_AUTO

        if cfg.pricing_mode == SELL_TEST_PRICING_AUTO:
            market_info = None
            if coord.market_info_cache is not None:
                try:
                    market_info = await coord.market_info_cache.get(cfg.token_id)
                except Exception:  # noqa: BLE001
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
                    {"event": "validation_harness_pricing", **resolved.to_evidence()},
                )
            )
            return resolved.price if resolved.source != "fallback" or resolved.price else cfg.limit_price
    return cfg.limit_price


def _seed_allocation_if_configured(
    app,
    coord,
    cfg: ValidationHarnessStrategyConfig,
    correlation_id: str,
) -> None:
    if not may_seed_allocation(app, cfg):
        return
    if cfg.seed_allocation_qty is None or cfg.seed_allocation_qty <= 0:
        return
    ledger = coord.allocation_ledger
    if ledger is None:
        return
    tid = TokenId(cfg.token_id)
    ledger.apply_buy(
        cfg.owner_id,
        tid,
        cfg.seed_allocation_qty,
        correlation_id=correlation_id,
    )
    entry = cfg.entry_price or cfg.limit_price or Decimal("0.50")
    coord.wallet.positions[tid] = WalletPosition(
        token_id=tid,
        qty=cfg.seed_allocation_qty,
        avg_price_usd=entry,
    )


def _default_fixture_prices(cfg: ValidationHarnessStrategyConfig, entry: Decimal) -> tuple[Decimal, Decimal]:
    if cfg.fixture_book_bid is not None and cfg.fixture_book_ask is not None:
        return cfg.fixture_book_bid, cfg.fixture_book_ask
    if cfg.mode == VALIDATION_MODE_PROTECTION_TP:
        return entry * Decimal("1.25"), entry * Decimal("1.26")
    if cfg.mode == VALIDATION_MODE_PROTECTION_SL:
        return entry * Decimal("0.75"), entry * Decimal("0.76")
    return entry - Decimal("0.01"), entry + Decimal("0.01")


def _maybe_inject_fixture_book(app, coord, cfg: ValidationHarnessStrategyConfig, *, stale: bool = False) -> None:
    if not may_use_fixture_book(app, cfg):
        return
    entry = cfg.limit_price or Decimal("0.50")
    if cfg.mode in (VALIDATION_MODE_PROTECTION_TP, VALIDATION_MODE_PROTECTION_SL):
        bid, ask = _default_fixture_prices(cfg, cfg.entry_price or entry)
        inject_fixture_book(coord, cfg.token_id, best_bid=bid, best_ask=ask, stale=stale)
    elif stale:
        inject_fixture_book(coord, cfg.token_id, best_bid=entry, best_ask=entry, stale=True)
    else:
        inject_fixture_book(
            coord,
            cfg.token_id,
            best_bid=entry,
            best_ask=entry + Decimal("0.02"),
            stale=False,
        )


async def _run_protection_register(
    *,
    app,
    run_id: RunId,
    coord,
    sink,
    oms,
    cfg: ValidationHarnessStrategyConfig,
    strategy: ValidationHarnessStrategy,
    apply_local_shadow_fill: bool,
    live_clob_client: object | None,
    corr_seed: str,
) -> int:
    limit_price = await _resolve_entry_limit_price(
        cfg, app=app, coord=coord, live_clob_client=live_clob_client, sink=sink, run_id=run_id
    )
    if limit_price is None or limit_price <= 0:
        log.error("protection_register requires positive limit_price")
        return 0
    since = None
    signal = build_validation_signal_from_config(cfg, limit_price=limit_price, side=Side.BUY)
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
    if cfg.wait_for_confirmed and not apply_local_shadow_fill:
        result = await wait_for_allocation_final(
            coord,
            token_id=TokenId(cfg.token_id),
            timeout_s=cfg.confirmed_timeout_s,
            apply_local_shadow_fill=False,
            sink=sink,
            run_id=str(run_id),
            correlation_id=corr_seed,
        )
        if result.final and result.status:
            register_protection_after_finality(
                coord,
                app,
                owner_id=cfg.owner_id,
                token_id=TokenId(cfg.token_id),
                entry_price=limit_price,
                correlation_id=corr_seed,
                run_id=run_id,
                sink=sink,
                finality_status=result.status,
            )
        elif cfg.fail_if_not_confirmed:
            log.error(
                "protection_register failed: allocation-final not observed within %.0fs",
                cfg.confirmed_timeout_s,
            )
            sink.write(
                make_fact(
                    FACT_TYPE_HEALTH,
                    str(run_id),
                    {
                        "event": "validation_harness_failed",
                        "mode": cfg.mode,
                        "reason": "finality_timeout",
                    },
                )
            )
    return 1


async def run_validation_harness_once(
    *,
    app,
    run_id: RunId,
    coord,
    sink,
    oms,
    cfg: ValidationHarnessStrategyConfig,
    apply_local_shadow_fill: bool = True,
    live_clob_client: object | None = None,
) -> int:
    """Run one validation harness scenario; return iteration count."""
    if not cfg.enabled:
        return 0

    validate_validation_harness_live_config(app)

    mode = cfg.mode
    strategy = ValidationHarnessStrategy(owner_id=cfg.owner_id)

    if mode == VALIDATION_MODE_MARKET_DATA_READONLY:
        return await run_market_data_readonly(
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            live_clob_client=live_clob_client,
            duration_s=cfg.market_data_readonly_seconds,
        )

    if app.runtime.market_data.enabled:
        ensure_market_state_store(coord, app)
        if live_clob_client is not None:
            await bootstrap_market_state(coord, app, live_clob_client=live_clob_client)

    if app.protection is not None and app.protection.enabled:
        init_protection_monitor(coord)

    corr_seed = f"validation_harness:{mode}:{cfg.token_id}"
    _seed_allocation_if_configured(app, coord, cfg, corr_seed)

    if mode in (VALIDATION_MODE_PROTECTION_TP, VALIDATION_MODE_PROTECTION_SL):
        entry = cfg.entry_price or cfg.limit_price or Decimal("0.50")
        if app.protection is None or not app.protection.enabled:
            log.error("protection_trigger modes require protection.enabled=true")
            return 0
        register_protection_manual(
            coord,
            app,
            owner_id=cfg.owner_id,
            token_id=TokenId(cfg.token_id),
            entry_price=entry,
            correlation_id=corr_seed,
            run_id=run_id,
            sink=sink,
        )
        _maybe_inject_fixture_book(app, coord, cfg)
        await run_protection_tick(
            coord,
            app,
            run_id=run_id,
            strategy=strategy,
            sink=sink,
            oms=oms,
            apply_local_shadow_fill=apply_local_shadow_fill,
            live_clob_client=live_clob_client,
        )
        return 1

    if mode == VALIDATION_MODE_PROTECTION_TRIGGER_LIVE:
        if app.protection is None or not app.protection.enabled:
            log.error("protection_trigger_live requires protection.enabled=true")
            return 0
        mon = init_protection_monitor(coord)
        if mon is not None and not mon.registry.active_entries():
            register_protection_from_venue_position(
                coord,
                app,
                owner_id=cfg.owner_id,
                token_id=TokenId(cfg.token_id),
                correlation_id=corr_seed,
                run_id=run_id,
                sink=sink,
                entry_price=cfg.entry_price,
            )
        await run_protection_supervisor(
            coord,
            app,
            run_id=run_id,
            strategy=strategy,
            sink=sink,
            oms=oms,
            apply_local_shadow_fill=apply_local_shadow_fill,
            live_clob_client=live_clob_client,
        )
        return 1

    if mode == VALIDATION_MODE_STALE_BOOK_DENY:
        _maybe_inject_fixture_book(app, coord, cfg, stale=True)
        signal = build_validation_signal_from_config(
            cfg,
            mode=mode,
            side=Side.SELL,
            urgency=URGENCY_URGENT,
            dedup_key=f"{corr_seed}:stale_exit",
        )
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
        return 1

    if mode == VALIDATION_MODE_URGENT_EXIT:
        if app.runtime.execution_mode == ExecutionMode.LIVE:
            ok, preflight = preflight_urgent_exit(coord, cfg)
            emit_preflight(sink, str(run_id), preflight)
            if not ok:
                log.error("urgent_exit preflight failed: %s", preflight)
                return 0
        elif may_use_fixture_book(app, cfg):
            _maybe_inject_fixture_book(app, coord, cfg)
        elif live_clob_client is not None:
            await bootstrap_market_state(coord, app, live_clob_client=live_clob_client)
        sell_size = cfg.size
        if sell_size is None and coord.allocation_ledger is not None:
            sell_size = coord.allocation_ledger.get_available_allocated(
                cfg.owner_id, TokenId(cfg.token_id)
            )
        signal = build_validation_signal_from_config(
            cfg,
            mode=mode,
            side=Side.SELL,
            urgency=URGENCY_URGENT,
            dedup_key=f"{corr_seed}:urgent_exit",
            size=sell_size if sell_size is not None and sell_size > 0 else None,
        )
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
        return 1

    if mode == VALIDATION_MODE_PROTECTION_REGISTER:
        return await _run_protection_register(
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            oms=oms,
            cfg=cfg,
            strategy=strategy,
            apply_local_shadow_fill=apply_local_shadow_fill,
            live_clob_client=live_clob_client,
            corr_seed=corr_seed,
        )

    limit_price = await _resolve_entry_limit_price(
        cfg, app=app, coord=coord, live_clob_client=live_clob_client, sink=sink, run_id=run_id
    )
    signal = build_validation_signal_from_config(cfg, limit_price=limit_price, side=Side.BUY)
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
    return 1
