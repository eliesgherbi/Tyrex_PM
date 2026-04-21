from __future__ import annotations

import asyncio
import logging

from tyrex_pm.core.time import monotonic_s, utc_now
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_HEALTH
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import AppConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.execution.order_lifecycle import sync_local_open_orders_from_venue_wallet
from tyrex_pm.runtime.pipeline import emit_wallet_sync, reconcile_coordinator
from tyrex_pm.venue.polymarket.clob_bridge import PyClobBridge
from tyrex_pm.venue.polymarket.clob_heartbeat import post_heartbeat_with_recovery
from tyrex_pm.venue.polymarket.clob_wallet_sync import refresh_wallet_from_clob
from tyrex_pm.venue.polymarket.data_api_client import DataApiClient
from tyrex_pm.venue.polymarket.positions_sync import refresh_positions_from_data_api

log = logging.getLogger(__name__)


def _heartbeat_interval_clamped(interval_s: float) -> float:
    """Polymarket rejects rapid repeat heartbeats; 0 or tiny intervals look like alternating 200/400."""
    v = float(interval_s)
    return max(v, 5.0)


async def supervised_heartbeat_loop(
    health: HealthRuntime,
    bridge: PyClobBridge,
    interval_s: float,
    sink: JsonlSink,
    *,
    run_id: str,
    stop: asyncio.Event,
) -> None:
    interval_s = _heartbeat_interval_clamped(interval_s)
    while not stop.is_set():
        ok = await post_heartbeat_with_recovery(health, bridge)
        if not ok:
            log.warning("CLOB heartbeat tick failed after server-id recovery")
        prev = health.heartbeat_ok
        health.mark_heartbeat(ok=ok)
        if ok != prev:
            sink.write(
                make_fact(
                    FACT_TYPE_HEALTH,
                    run_id,
                    {"event": "heartbeat", "heartbeat_ok": ok},
                )
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return
        except asyncio.TimeoutError:
            continue


async def venue_refresh_loop(
    coord: RuntimeCoordinator,
    clob_client: object,
    interval_s: float,
    sink: JsonlSink,
    run_id: str,
    stop: asyncio.Event,
    *,
    positions_client: DataApiClient | None = None,
    positions_wallet_address: str | None = None,
) -> None:
    """Periodic REST refresh: open orders + (optionally) positions, then full reconcile.

    When ``positions_client`` and ``positions_wallet_address`` are supplied, the loop also
    pulls the canonical positions snapshot from data-api/positions and replaces
    ``WalletStore.positions``. This bridges WS-trade gaps that can otherwise leave the
    deployment evaluator without marks (or with ghost shorts) for an unbounded time.
    """
    while not stop.is_set():
        try:
            await refresh_wallet_from_clob(coord.wallet, clob_client)
            if positions_client is not None and positions_wallet_address:
                await refresh_positions_from_data_api(
                    coord.wallet, positions_client, positions_wallet_address
                )
            sync_local_open_orders_from_venue_wallet(coord.orders, coord.wallet)
            # Emit wallet_sync BEFORE reconcile so the change-of-state evidence
            # appears chronologically alongside the reconcile that consumed it.
            emit_wallet_sync(coord, sink, run_id)
            reconcile_coordinator(coord, sink, run_id)
            # V2 bootstrap gate: open the door for new-order risk evaluation
            # only after the first venue truth rebuild completes successfully.
            if not coord.health.first_v2_sync_complete:
                coord.health.mark_first_v2_sync_complete()
                sink.write(
                    make_fact(
                        FACT_TYPE_HEALTH,
                        run_id,
                        {"event": "first_v2_sync_complete"},
                    )
                )
        except Exception:
            log.exception("venue refresh failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return
        except asyncio.TimeoutError:
            continue


async def provisional_repair_probe_loop(
    coord: RuntimeCoordinator,
    clob_client: object,
    sink: JsonlSink,
    run_id: str,
    stop: asyncio.Event,
    *,
    schedule_s: tuple[float, ...] = (1.0, 5.0, 15.0),
) -> None:
    """Short-cadence REST repair + reconcile pass while any provisional row exists.

    Wakes on the (1, 5, 15) schedule when there are unconfirmed provisional rows; otherwise sleeps
    on the longest interval. This complements ``venue_refresh_loop`` (every reconcile_interval_s)
    by giving fresh provisional rows a faster path to either ``venue_confirmed`` (REST repair),
    ``filled_resolved`` (trade evidence), or ``UNKNOWN_TERMINAL`` (timeout).
    """
    sched = tuple(schedule_s) or (1.0, 5.0, 15.0)
    idle_s = sched[-1]
    while not stop.is_set():
        provisionals = [
            o for o in coord.orders.orders.values() if o.confirmation == "provisional"
        ]
        if not provisionals:
            try:
                await asyncio.wait_for(stop.wait(), timeout=idle_s)
                return
            except asyncio.TimeoutError:
                continue
        for delay in sched:
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await refresh_wallet_from_clob(coord.wallet, clob_client)
                sync_local_open_orders_from_venue_wallet(coord.orders, coord.wallet)
                reconcile_coordinator(coord, sink, run_id)
            except Exception:
                log.exception("provisional repair probe failed")
            still_provisional = any(
                o.confirmation == "provisional" for o in coord.orders.orders.values()
            )
            if not still_provisional:
                break


async def user_ws_staleness_loop(
    health: HealthRuntime,
    app: AppConfig,
    *,
    threshold_s: float,
    grace_s: float,
    sink: JsonlSink,
    run_id: str,
    stop: asyncio.Event,
) -> None:
    """Mark venue user-stream truth stale if messages stop (fail-closed for new live orders)."""
    if health.user_ws_rest_only or not app.risk.readiness.require_user_ws_live:
        await stop.wait()
        return
    started = monotonic_s()
    prev = health.venue_truth_stale
    while not stop.is_set():
        now_m = monotonic_s()
        if health.user_ws_last_msg_ts is None:
            stale = now_m - started > grace_s
        else:
            age = (utc_now() - health.user_ws_last_msg_ts).total_seconds()
            stale = age > threshold_s
        health.venue_truth_stale = stale
        if stale != prev:
            sink.write(
                make_fact(
                    FACT_TYPE_HEALTH,
                    run_id,
                    {"event": "user_ws_stale", "venue_truth_stale": stale},
                )
            )
            prev = stale
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
            return
        except asyncio.TimeoutError:
            continue
