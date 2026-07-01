"""Market data runtime wiring (P4.5 architecture_enhance).

Attaches :class:`MarketStateStore` to the coordinator when ``market_data.enabled``
and optionally bootstraps/refreshes books from REST.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from decimal import Decimal
from typing import Iterable

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.time import monotonic_s, utc_now
from tyrex_pm.market_data.models import BookSource
from tyrex_pm.market_data.decision_gate import rest_poll_should_run, ws_primary_enabled
from tyrex_pm.market_data.readiness_runtime import (
    ensure_market_readiness_tracker,
    emit_market_data_health_block,
    refresh_market_readiness,
)
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_HEALTH
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import AppConfig
from tyrex_pm.state.market_store import BookLevel, MarketStateStore, make_snapshot
from tyrex_pm.venue.polymarket.book_snapshot import bootstrap_market_store_from_rest

log = logging.getLogger(__name__)


def ensure_market_state_store(coord, app: AppConfig) -> MarketStateStore:
    """Create or return the coordinator's market store when market data is enabled."""
    if coord.market_state is None:
        coord.market_state = MarketStateStore(
            default_max_age_s=float(app.runtime.market_data.max_book_age_s),
            store_top_n_levels=int(app.runtime.market_data.store_top_n_levels),
        )
    return coord.market_state  # type: ignore[return-value]


def ensure_market_state_shadow_store(coord, app: AppConfig) -> MarketStateStore:
    """Create the WS shadow store — never wired to strategy/risk/planner."""
    if coord.market_state_shadow is None:
        coord.market_state_shadow = MarketStateStore(
            default_max_age_s=float(app.runtime.market_data.max_book_age_s),
            store_top_n_levels=int(app.runtime.market_data.store_top_n_levels),
        )
    return coord.market_state_shadow  # type: ignore[return-value]


def resolve_market_token_ids(app: AppConfig) -> list[str]:
    """Token ids to bootstrap: explicit config + validation harness token."""
    ids: list[str] = list(app.runtime.market_data.token_ids)
    vh = getattr(app, "validation_harness", None)
    if vh is not None and vh.token_id and vh.token_id not in ids:
        ids.append(vh.token_id)
    pb = getattr(app, "paired_binary", None)
    if pb is not None:
        for tid in (pb.yes_token_id, pb.no_token_id):
            if tid and tid not in ids:
                ids.append(tid)
    return ids


async def bootstrap_market_state(
    coord,
    app: AppConfig,
    *,
    live_clob_client: object | None = None,
    source: str = BookSource.REST_BOOTSTRAP,
) -> int:
    """REST-bootstrap configured tokens into ``coord.market_state``."""
    if not app.runtime.market_data.enabled:
        return 0
    store = ensure_market_state_store(coord, app)
    token_ids = resolve_market_token_ids(app)
    if not token_ids:
        return 0
    if live_clob_client is None:
        return 0
    return await bootstrap_market_store_from_rest(store, live_clob_client, token_ids, source=source)


def inject_fixture_book(
    coord,
    token_id: str | TokenId,
    *,
    best_bid: Decimal,
    best_ask: Decimal,
    stale: bool = False,
    size: Decimal = Decimal("10000"),
) -> None:
    """Apply a synthetic book snapshot (shadow / harness fixture pricing)."""
    store = coord.market_state
    if store is None:
        raise RuntimeError("market_state not initialized")
    tid = TokenId(str(token_id))
    ts = utc_now() - timedelta(seconds=60) if stale else utc_now()
    store.apply_book(
        tid,
        [BookLevel(best_bid, size)],
        [BookLevel(best_ask, size)],
        source=BookSource.FIXTURE,
        received_ts=ts,
    )


def market_store_health_payload(coord, token_id: str | TokenId) -> dict:
    store = coord.market_state
    tid = TokenId(str(token_id))
    if store is None:
        return {"market_state": "missing"}
    snap = store.snapshot(tid)
    max_age = getattr(store, "default_max_age_s", 5.0)
    return {
        "event": "market_data_snapshot",
        "token_id": str(tid),
        "best_bid": str(store.best_bid(tid)) if store.best_bid(tid) is not None else None,
        "best_ask": str(store.best_ask(tid)) if store.best_ask(tid) is not None else None,
        "mid": str(store.mid(tid)) if store.mid(tid) is not None else None,
        "is_stale": store.is_stale(tid, max_age_s=max_age),
        "has_snapshot": snap is not None,
    }


async def run_market_data_readonly(
    *,
    app: AppConfig,
    run_id: RunId,
    coord,
    sink: JsonlSink,
    live_clob_client: object | None = None,
    duration_s: float = 3.0,
) -> int:
    """Level 2 smoke: populate store, emit snapshot health, exit without orders."""
    if not app.runtime.market_data.enabled:
        log.warning("market_data_readonly requested but market_data.enabled=false")
        return 0
    store = ensure_market_state_store(coord, app)
    vh = app.validation_harness
    token_id = vh.token_id if vh else (resolve_market_token_ids(app)[0] if resolve_market_token_ids(app) else "")
    if live_clob_client is not None and token_id:
        await bootstrap_market_store_from_rest(store, live_clob_client, [token_id])
    elif token_id and store.snapshot(TokenId(token_id)) is None:
        inject_fixture_book(coord, token_id, best_bid=Decimal("0.19"), best_ask=Decimal("0.21"))

    sink.write(
        make_fact(
            FACT_TYPE_HEALTH,
            str(run_id),
            market_store_health_payload(coord, token_id),
        )
    )
    if duration_s > 0:
        await asyncio.sleep(duration_s)
        sink.write(
            make_fact(
                FACT_TYPE_HEALTH,
                str(run_id),
                {**market_store_health_payload(coord, token_id), "phase": "after_wait"},
            )
        )
    return 1


async def market_data_rest_refresh_loop(
    coord,
    app: AppConfig,
    live_clob_client: object,
    *,
    stop: asyncio.Event,
    interval_s: float = 5.0,
    ws_connected_ref: dict[str, bool] | None = None,
) -> None:
    """Periodic REST book refresh — skipped when WS-primary healthy and poll disabled."""

    def _ws_connected() -> bool:
        if ws_connected_ref is None:
            return False
        return bool(ws_connected_ref.get("connected", False))

    if not app.runtime.market_data.rest.poll_enabled:
        log.info("REST poll loop not started: poll_enabled=false")
        return
    if ws_primary_enabled(app) and not rest_poll_should_run(app, ws_connected=_ws_connected()):
        log.info("REST poll loop not started: WS-primary healthy steady state")
        return
    while not stop.is_set():
        if ws_primary_enabled(app) and not rest_poll_should_run(app, ws_connected=_ws_connected()):
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await bootstrap_market_state(
                coord, app, live_clob_client=live_clob_client, source=BookSource.REST_POLL
            )
        except Exception:
            log.exception("market_data REST refresh failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
