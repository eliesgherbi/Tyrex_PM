"""Sigma warm-up and Binance history seeding for Z-Gap session orchestrator."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from tyrex_pm.core.ids import RunId
from tyrex_pm.quant.model_sanity import (
    ModelSanityConfig,
    simple_realized_sigma_per_sqrt_second,
    sigma_ratio_warning,
)
from tyrex_pm.quant.volatility import EwmaVolatilityEstimator, SeedResult, SigmaConfig
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_Z_GAP_SIGMA_WARMUP
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import AppConfig, ZGapSigmaConfig
from tyrex_pm.runtime.z_gap_run import ObserveTickContext, resolve_fee_model, run_observe_tick, sigma_config_from_zg

log = logging.getLogger(__name__)

_BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
_BINANCE_AGGTRADES_URL = "https://api.binance.com/api/v3/aggTrades"
_DEFAULT_FEED_CONNECT_RESERVE_S = 25.0

SIGMA_WARM = "SIGMA_WARM"
SIGMA_WARMING = "SIGMA_WARMING"
SIGMA_NOT_READY = "SIGMA_NOT_READY"

PROVENANCE_LIVE_ONLY = "live_only"
PROVENANCE_SEEDED_THEN_LIVE = "seeded_then_live"


@dataclass
class SigmaWarmupState:
    status: str = SIGMA_NOT_READY
    sigma_value: float | None = None
    sample_count: int = 0
    min_samples: float = 20.0
    warmup_started_at: str | None = None
    warmup_duration_ms: float | None = None
    seed_sample_count: int = 0
    seed_start_ts: str | None = None
    seed_end_ts: str | None = None
    provenance: str = PROVENANCE_LIVE_ONLY
    ready_at: str | None = None
    simple_sigma_1s: float | None = None
    sigma_ratio_ewma_over_simple: float | None = None
    seed_source: str | None = None

    def to_report_payload(self) -> dict[str, Any]:
        return {
            "sigma_status": self.status,
            "sigma_value": self.sigma_value,
            "sigma_sample_count": self.sample_count,
            "sigma_min_samples": self.min_samples,
            "sigma_warmup_started_at": self.warmup_started_at,
            "sigma_warmup_duration_ms": self.warmup_duration_ms,
            "sigma_seed_sample_count": self.seed_sample_count,
            "sigma_seed_start_ts": self.seed_start_ts,
            "sigma_seed_end_ts": self.seed_end_ts,
            "sigma_provenance": self.provenance,
            "sigma_ready_at": self.ready_at,
            "simple_sigma_1s": self.simple_sigma_1s,
            "sigma_ratio_ewma_over_simple": self.sigma_ratio_ewma_over_simple,
            "seed_source": self.seed_source,
        }


def sigma_warmup_required_seconds(
    sigma: ZGapSigmaConfig | SigmaConfig,
    *,
    feed_connect_reserve_s: float = _DEFAULT_FEED_CONNECT_RESERVE_S,
) -> float:
    """Minimum pre-boundary lead time so live EWMA can reach ``min_samples_s`` after feeds connect."""
    min_samples = float(sigma.min_samples_s)
    interval = float(sigma.sample_interval_s)
    return feed_connect_reserve_s + min_samples + interval


def sigma_status_from_snapshot(*, ready: bool, sample_count: int) -> str:
    if ready:
        return SIGMA_WARM
    if sample_count > 0:
        return SIGMA_WARMING
    return SIGMA_NOT_READY


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def _resample_observations_to_interval(
    observations: list[tuple[Decimal, datetime]],
    *,
    sample_interval_s: float,
) -> list[tuple[Decimal, datetime]]:
    """Collapse high-frequency ticks to one last-price sample per interval bucket."""
    if not observations:
        return []
    interval = max(float(sample_interval_s), 0.001)
    ordered = sorted(observations, key=lambda row: row[1])
    buckets: dict[int, tuple[Decimal, datetime]] = {}
    for price, ts in ordered:
        bucket_key = int(ts.timestamp() // interval)
        buckets[bucket_key] = (price, ts)
    return [buckets[key] for key in sorted(buckets.keys())]


def _filter_duplicate_prices(
    observations: list[tuple[Decimal, datetime]],
) -> list[tuple[Decimal, datetime]]:
    filtered: list[tuple[Decimal, datetime]] = []
    last_px: Decimal | None = None
    for price, ts in observations:
        if last_px is not None and price == last_px:
            continue
        filtered.append((price, ts))
        last_px = price
    return filtered


def _fetch_binance_agg_trades_sync(
    *,
    symbol: str,
    limit: int,
    timeout_s: float = 10.0,
) -> list[tuple[Decimal, datetime]]:
    resp = httpx.get(
        _BINANCE_AGGTRADES_URL,
        params={"symbol": symbol.upper(), "limit": limit},
        timeout=timeout_s,
    )
    resp.raise_for_status()
    rows = resp.json()
    out: list[tuple[Decimal, datetime]] = []
    for row in rows:
        trade_ms = int(row["T"])
        price = Decimal(str(row["p"]))
        ts = datetime.fromtimestamp(trade_ms / 1000.0, tz=timezone.utc)
        out.append((price, ts))
    return out


def _fetch_binance_klines_sync(
    *,
    symbol: str,
    limit: int,
    interval: str = "1s",
    timeout_s: float = 10.0,
) -> list[tuple[Decimal, datetime]]:
    resp = httpx.get(
        _BINANCE_KLINES_URL,
        params={"symbol": symbol.upper(), "interval": interval, "limit": limit},
        timeout=timeout_s,
    )
    resp.raise_for_status()
    rows = resp.json()
    out: list[tuple[Decimal, datetime]] = []
    for row in rows:
        open_ms = int(row[0])
        close_px = Decimal(str(row[4]))
        ts = datetime.fromtimestamp(open_ms / 1000.0, tz=timezone.utc)
        out.append((close_px, ts))
    return out


async def fetch_recent_binance_trade_observations(
    *,
    symbol: str,
    min_samples_s: float,
    sample_interval_s: float,
) -> list[tuple[Decimal, datetime]]:
    """Fetch recent aggTrade ticks (trade price, exchange time in ms)."""
    needed = int(min_samples_s / max(sample_interval_s, 0.001)) + 20
    limit = min(max(needed * 4, 100), 1000)
    return await asyncio.to_thread(
        _fetch_binance_agg_trades_sync,
        symbol=symbol,
        limit=limit,
    )


async def fetch_recent_binance_close_observations(
    *,
    symbol: str,
    min_samples_s: float,
    sample_interval_s: float,
) -> list[tuple[Decimal, datetime]]:
    """Fetch recent 1s kline closes for the same instrument used by live Binance ingest."""
    needed = int(min_samples_s / max(sample_interval_s, 0.001)) + 5
    limit = min(max(needed, 25), 1000)
    return await asyncio.to_thread(
        _fetch_binance_klines_sync,
        symbol=symbol,
        limit=limit,
        interval="1s",
    )


def emit_sigma_warmup_fact(
    sink: JsonlSink | object,
    run_id: RunId,
    state: SigmaWarmupState,
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    payload = state.to_report_payload()
    if extra:
        payload.update(extra)
    sink.write(make_fact(FACT_TYPE_Z_GAP_SIGMA_WARMUP, str(run_id), payload))


async def seed_sigma_from_binance_history(
    estimator: EwmaVolatilityEstimator,
    *,
    symbol: str,
    now_dt: datetime,
    max_age_s: float | None = None,
) -> tuple[SeedResult | None, list[tuple[Decimal, datetime]], str | None]:
    cfg = estimator.config
    observations: list[tuple[Decimal, datetime]] = []
    seed_source: str | None = None
    try:
        observations = await fetch_recent_binance_trade_observations(
            symbol=symbol,
            min_samples_s=cfg.min_samples_s,
            sample_interval_s=cfg.sample_interval_s,
        )
        seed_source = "aggTrade"
    except Exception:
        log.warning("sigma seed: Binance aggTrades fetch failed", exc_info=True)
    if not observations:
        try:
            observations = await fetch_recent_binance_close_observations(
                symbol=symbol,
                min_samples_s=cfg.min_samples_s,
                sample_interval_s=cfg.sample_interval_s,
            )
            seed_source = "kline_1s"
        except Exception:
            log.warning("sigma seed: Binance klines fetch failed", exc_info=True)
            return None, [], None

    if max_age_s is not None:
        cutoff = now_dt.timestamp() - max_age_s
        observations = [(px, ts) for px, ts in observations if ts.timestamp() >= cutoff]

    if not observations:
        return None, [], seed_source

    resampled = _resample_observations_to_interval(
        observations,
        sample_interval_s=cfg.sample_interval_s,
    )
    filtered = _filter_duplicate_prices(resampled)
    if not filtered:
        return None, [], seed_source

    result = estimator.seed_observations(filtered, now_ts=now_dt)
    return result, filtered, seed_source


async def warm_sigma_before_boundary(
    handle: Any,
    app: AppConfig,
    *,
    tick_interval_s: float = 0.5,
) -> bool:
    """Seed EWMA from recent Binance klines, then continue warming with live ticks until boundary."""
    assert app.z_gap is not None
    zg = app.z_gap
    sigma_cfg = sigma_config_from_zg(zg)
    if handle.sigma_estimator is None:
        handle.sigma_estimator = EwmaVolatilityEstimator(config=sigma_cfg)
    estimator: EwmaVolatilityEstimator = handle.sigma_estimator

    warmup = SigmaWarmupState(min_samples=float(sigma_cfg.min_samples_s))
    warmup.warmup_started_at = datetime.now(timezone.utc).isoformat()
    handle.sigma_warmup = warmup

    symbol = app.runtime.external_btc.symbol
    ta = handle.coord.time_authority
    now_dt = (
        datetime.fromtimestamp(ta.corrected_epoch(), tz=timezone.utc)
        if ta is not None
        else datetime.now(timezone.utc)
    )

    seed_result, seed_observations, seed_source = await seed_sigma_from_binance_history(
        estimator,
        symbol=symbol,
        now_dt=now_dt,
        max_age_s=sigma_cfg.min_samples_s + sigma_cfg.sample_interval_s + 30.0,
    )
    if seed_result is not None and seed_result.accepted > 0:
        warmup.provenance = PROVENANCE_SEEDED_THEN_LIVE
        warmup.seed_sample_count = seed_result.accepted
        warmup.seed_start_ts = _iso(seed_result.start_ts)
        warmup.seed_end_ts = _iso(seed_result.end_ts)
        warmup.seed_source = seed_source
        simple_sigma = simple_realized_sigma_per_sqrt_second(
            seed_observations,
            sample_interval_s=sigma_cfg.sample_interval_s,
        )
        warmup.simple_sigma_1s = simple_sigma
        snap_after_seed = estimator.snapshot()
        if simple_sigma is not None and snap_after_seed.sigma is not None and simple_sigma > 0:
            warmup.sigma_ratio_ewma_over_simple = snap_after_seed.sigma / simple_sigma
        ratio_issue = sigma_ratio_warning(
            ewma_sigma=snap_after_seed.sigma,
            simple_sigma=simple_sigma,
            cfg=ModelSanityConfig(),
        )
        if ratio_issue:
            log.warning("sigma seed diagnostic: %s", ratio_issue)
        log.info(
            "sigma seed: accepted=%s source=%s span=%s..%s simple_sigma=%s ewma_sigma=%s",
            seed_result.accepted,
            seed_source,
            warmup.seed_start_ts,
            warmup.seed_end_ts,
            simple_sigma,
            snap_after_seed.sigma,
        )

    fee_model = await resolve_fee_model(handle.coord, zg)
    entry_cfg = zg.entry
    t0 = time.monotonic()

    def _sync_state() -> None:
        snap = estimator.snapshot()
        warmup.sigma_value = snap.sigma
        warmup.sample_count = snap.sample_count
        warmup.status = sigma_status_from_snapshot(ready=snap.ready, sample_count=snap.sample_count)
        if snap.ready and warmup.ready_at is None:
            warmup.ready_at = datetime.now(timezone.utc).isoformat()
        handle.sigma_warm = snap.ready

    _sync_state()
    emit_sigma_warmup_fact(handle.sink, handle.run_id, warmup)

    while True:
        if handle.stop.is_set():
            break
        if zg.event_start_ts is not None and ta is not None and ta.corrected_epoch() >= float(zg.event_start_ts):
            break

        ctx = ObserveTickContext(
            app=app,
            zg=zg,
            run_id=handle.run_id,
            coord=handle.coord,
            sink=handle.sink,
            state=handle.observe_state,
            estimator=estimator,
            fee_model=fee_model,
            time_authority=ta,
            entry_cfg=entry_cfg,
        )
        run_observe_tick(ctx, allow_enforce=False)
        _sync_state()

        if estimator.snapshot().ready:
            warmup.warmup_duration_ms = round((time.monotonic() - t0) * 1000.0, 1)
            handle.startup.sigma_warmup_ms = warmup.warmup_duration_ms
            emit_sigma_warmup_fact(handle.sink, handle.run_id, warmup, extra={"phase": "pre_boundary_ready"})
            return True

        await asyncio.sleep(tick_interval_s)

    warmup.warmup_duration_ms = round((time.monotonic() - t0) * 1000.0, 1)
    handle.startup.sigma_warmup_ms = warmup.warmup_duration_ms
    handle.sigma_warm = estimator.snapshot().ready
    emit_sigma_warmup_fact(handle.sink, handle.run_id, warmup, extra={"phase": "pre_boundary_boundary_reached"})
    return handle.sigma_warm
