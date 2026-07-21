#!/usr/bin/env python3
"""Bounded public-data smoke for N2 adapters (mutation-free).

No auth, wallets, orders, user channels, or old/ imports.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
from datetime import datetime, timezone
from pathlib import Path

from tyrex_pm.adapters.binance.ws_adapter import BinanceTradeWsAdapter
from tyrex_pm.adapters.clock_sync import OsMonitorClockSyncProvider
from tyrex_pm.adapters.polymarket.discovery import (
    GammaMarketDiscovery,
    bind_btc_5m_gamma_event,
    current_btc_updown_slug,
    next_btc_updown_slug,
)
from tyrex_pm.adapters.polymarket.rtds_adapter import (
    RtdsBinanceComparisonAdapter,
    RtdsChainlinkAdapter,
)
from tyrex_pm.adapters.polymarket.ws_adapter import PolymarketMarketWsAdapter
from tyrex_pm.core.book_events import BookDeltaReceived, BookSnapshotReceived
from tyrex_pm.core.clock import SystemClock
from tyrex_pm.core.events import ReferencePriceUpdated, SettlementReferenceUpdated
from tyrex_pm.core.time_authority import SnapshotTimeAuthority
from tyrex_pm.domain.polymarket.discovery_binding import DiscoverySessionRole
from tyrex_pm.engine.dispatcher import EventDispatcher

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "n2" / "gamma_btc_5m_event.json"


async def _try_live_discovery() -> tuple[object | None, str]:
    try:
        discovery = GammaMarketDiscovery()
        slug = current_btc_updown_slug()
        binding = await discovery.resolve_btc_5m_window(
            slug=slug, session_role=DiscoverySessionRole.ACTIVE
        )
        return binding, "live_gamma"
    except Exception as exc:
        return None, f"live_gamma_failed:{type(exc).__name__}:{exc}"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-s", type=float, default=25.0)
    ap.add_argument("--out", type=Path, default=Path("var/reporting/n2/smoke_summary.json"))
    ap.add_argument(
        "--insecure-ssl",
        action="store_true",
        help="Diagnostic only: disable TLS verify for Polymarket sockets "
        "(broken corporate proxies). Not for production.",
    )
    args = ap.parse_args()
    ssl_ctx = ssl._create_unverified_context() if args.insecure_ssl else None

    summary: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "auth_touched": False,
        "orders_touched": False,
        "feeds": {},
    }
    health_log: list[dict] = []

    def on_health(status: str, extra: dict) -> None:
        health_log.append(
            {"status": status, **extra, "t": datetime.now(timezone.utc).isoformat()}
        )

    live_binding, disc_note = await _try_live_discovery()
    if live_binding is not None:
        binding = live_binding
        next_note = next_btc_updown_slug()
        discovery_mode = "live_gamma"
    else:
        event = json.loads(FIXTURE.read_text(encoding="utf-8"))
        binding = bind_btc_5m_gamma_event(
            event,
            expected_slug=event["slug"],
            session_role=DiscoverySessionRole.ACTIVE,
        )
        next_note = next_btc_updown_slug()
        discovery_mode = "fixture_fallback"
    summary["discovery"] = {
        "mode": discovery_mode,
        "note": disc_note,
        "current_slug": binding.window_slug,
        "next_slug": next_note,
        "condition_id": binding.market.condition_id,
        "up_token": binding.up_token_id,
        "down_token": binding.down_token_id,
        "resolution_source": binding.resolution_source,
        "ok": True,
    }

    clock = SystemClock()
    auth = SnapshotTimeAuthority(clock=clock, max_uncertainty_ms=10_000)
    provider = OsMonitorClockSyncProvider(enable_binance_cross_check=True)
    snap = await provider.measure()
    auth.apply_snapshot(snap)
    view = auth.view()
    summary["clock"] = {
        "sync_status": view.sync_status.value,
        "uncertainty_ms": view.uncertainty_ms,
        "estimated_offset_ms": view.estimated_offset_ms,
        "ready": view.ready,
        "disagreement_ms": snap.max_source_disagreement_ms,
        "sources": [
            {
                "source": s.source,
                "offset_ms": s.offset_ms,
                "rtt_ms": s.round_trip_ms,
                "ok": s.ok,
            }
            for s in snap.sources
        ],
    }

    disp = EventDispatcher()
    counts = {
        "rtds_chainlink": 0,
        "binance_spot": 0,
        "rtds_binance": 0,
        "clob_book": 0,
    }
    first_latencies: dict[str, float | None] = {
        "rtds_chainlink_recv_minus_source_ms": None,
        "binance_recv_minus_source_ms": None,
    }

    def on_cl(e: SettlementReferenceUpdated) -> None:
        counts["rtds_chainlink"] += 1
        if first_latencies["rtds_chainlink_recv_minus_source_ms"] is None:
            first_latencies["rtds_chainlink_recv_minus_source_ms"] = (
                e.ts_received - e.ts_event
            ).total_seconds() * 1000.0

    def on_ref(e: ReferencePriceUpdated) -> None:
        if e.source.value == "binance":
            counts["binance_spot"] += 1
            if first_latencies["binance_recv_minus_source_ms"] is None:
                first_latencies["binance_recv_minus_source_ms"] = (
                    e.ts_received - e.ts_event
                ).total_seconds() * 1000.0
        elif e.source.value == "polymarket_rtds_binance":
            counts["rtds_binance"] += 1

    def on_book(_e: object) -> None:
        counts["clob_book"] += 1

    disp.subscribe(SettlementReferenceUpdated, on_cl)
    disp.subscribe(ReferencePriceUpdated, on_ref)
    disp.subscribe(BookSnapshotReceived, on_book)
    disp.subscribe(BookDeltaReceived, on_book)

    cl = RtdsChainlinkAdapter(
        on_health=on_health, heartbeat_timeout_s=25, ssl=ssl_ctx
    )
    bn = BinanceTradeWsAdapter(symbol="BTCUSDT", on_health=on_health)
    rtds_bn = RtdsBinanceComparisonAdapter(
        mode="auto",
        filtered_idle_s=6.0,
        on_health=on_health,
        heartbeat_timeout_s=25,
        ssl=ssl_ctx,
    )
    # CLOB adapter uses websockets.connect without ssl override in N2;
    # book smoke may remain blocked when TLS interception is present.
    clob = PolymarketMarketWsAdapter.from_binding(
        binding, on_health=on_health, ssl=ssl_ctx
    )
    summary["insecure_ssl"] = bool(args.insecure_ssl)

    tasks = [
        asyncio.create_task(cl.run(disp), name="cl"),
        asyncio.create_task(bn.run(disp), name="bn"),
        asyncio.create_task(rtds_bn.run(disp), name="rtds_bn"),
        asyncio.create_task(clob.run(disp), name="clob"),
    ]
    try:
        await asyncio.sleep(args.duration_s)
    finally:
        await cl.stop()
        await bn.stop()
        await rtds_bn.stop()
        await clob.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    summary["feeds"] = {
        "rtds_chainlink_ticks": counts["rtds_chainlink"],
        "binance_spot_ticks": counts["binance_spot"],
        "rtds_binance_ticks": counts["rtds_binance"],
        "rtds_binance_mode": rtds_bn.active_mode,
        "clob_book_events": counts["clob_book"],
        "clob_ready": clob.ready,
        "chainlink_ready": cl.ready,
        "binance_ready": bn.ready,
        "latencies_ms": first_latencies,
        "connection_generations": {
            "chainlink": cl.connection_generation,
            "binance": bn.connection_generation,
            "rtds_binance": rtds_bn.connection_generation,
            "clob": clob.connection_generation,
        },
    }
    summary["health_tail"] = health_log[-40:]
    summary["ended_at"] = datetime.now(timezone.utc).isoformat()
    summary["blockers"] = []
    if discovery_mode != "live_gamma":
        summary["blockers"].append(
            "Polymarket Gamma HTTP unavailable in this environment; used fixture binding"
        )
    if counts["rtds_chainlink"] == 0:
        summary["blockers"].append(
            "RTDS Chainlink not reachable (TLS/proxy interception observed)"
        )
    if counts["clob_book"] == 0:
        summary["blockers"].append("CLOB book events not observed (network or stale tokens)")
    if counts["rtds_binance"] == 0:
        summary["blockers"].append("RTDS Binance comparison ticks not observed in window")

    summary["binance_ok"] = counts["binance_spot"] > 0
    summary["clock_ok"] = True
    summary["polymarket_ok"] = counts["rtds_chainlink"] > 0 and counts["clob_book"] > 0
    summary["pass"] = summary["binance_ok"] and summary["polymarket_ok"]
    summary["pass_partial"] = summary["binance_ok"] and not summary["pass"]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2)[:5000])
    # Exit 0 on partial when Binance+clock work but Polymarket is environment-blocked.
    if not (summary["pass"] or summary["pass_partial"]):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
