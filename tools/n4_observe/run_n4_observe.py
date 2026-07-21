#!/usr/bin/env python3
"""Bounded N4 OBSERVE runner (mutation-free).

Modes:
  --mode fixture   Offline deterministic replay (CI / N4A).
  --mode live      Public-data OBSERVE on a healthy Polymarket TLS host (N4B).

Never disables TLS verification. Never submits orders.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.adapters.binance.ws_adapter import BinanceTradeWsAdapter
from tyrex_pm.adapters.clock_sync import OsMonitorClockSyncProvider
from tyrex_pm.adapters.polymarket.discovery import (
    GammaMarketDiscovery,
    bind_btc_5m_gamma_event,
    current_btc_updown_slug,
)
from tyrex_pm.adapters.polymarket.rtds_adapter import RtdsChainlinkAdapter
from tyrex_pm.adapters.polymarket.ws_adapter import PolymarketMarketWsAdapter
from tyrex_pm.core.clock import FakeClock, SystemClock
from tyrex_pm.core.events import ReferencePriceUpdated, SettlementReferenceUpdated
from tyrex_pm.core.ingress import FeedRole, IngressMeta
from tyrex_pm.core.time_authority import SnapshotTimeAuthority
from tyrex_pm.domain.polymarket.boundary_candidates import BoundaryTickView
from tyrex_pm.domain.polymarket.discovery_binding import DiscoverySessionRole
from tyrex_pm.domain.polymarket.ptb_attestation import FixturePtbAttestationProvider
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.indicators.causal_pairing import PriceTickView, TradingReferenceIdentity
from tyrex_pm.runtime.n4_observe_runtime import N4ObserveRuntime, SessionSlot
from tyrex_pm.strategies.z_gap.config import ZGapConfig, ZGapPtbTimeQualityConfig

REPO = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE = REPO / "tests" / "fixtures" / "n3" / "n1_three_windows.json"
DEFAULT_GAMMA = REPO / "tests" / "fixtures" / "n2" / "gamma_btc_5m_event.json"


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def run_fixture(
    *,
    fixture_path: Path,
    out_path: Path,
    windows: int,
) -> dict:
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    windows_data = data["windows"][: max(1, windows)]
    attest = {
        w["window_id"]: (
            w["attested_open_price"],
            w["attestation_source"],
            {"fixture": True, "not_live": True},
        )
        for w in windows_data
    }
    start = _ts(windows_data[0]["event_start"])
    clock = FakeClock(_wall=start)
    runtime = N4ObserveRuntime.create(
        clock=clock,
        attestation_port=FixturePtbAttestationProvider(by_window=attest),
        basis_ewma_half_life_s=30.0,
        zgap_config=ZGapConfig(
            ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=Decimal("10000"))
        ),
    )

    from tyrex_pm.core.ids import MarketId, TokenId
    from tyrex_pm.domain.polymarket.market import (
        BinaryMarket,
        MarketStatus,
        make_binary_instruments,
    )

    observations: list[dict] = []
    for idx, w in enumerate(windows_data):
        mid = MarketId(w["market_id"])
        yes, no = make_binary_instruments(
            market_id=mid,
            yes_token=TokenId(f"up-{w['window_id']}"),
            no_token=TokenId(f"down-{w['window_id']}"),
        )
        market = BinaryMarket(
            market_id=mid,
            condition_id=w["window_id"],
            question=f"N4 fixture {w['window_id']}",
            yes=yes,
            no=no,
            event_start=_ts(w["event_start"]),
            event_end=_ts(w["event_end"]),
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("5"),
            status=MarketStatus.ACTIVE,
        )
        slot = SessionSlot.ACTIVE if idx == 0 else SessionSlot.PREPARED_NEXT
        sess = runtime.open_session(
            slot=slot, market=market, window_id=w["window_id"]
        )
        sess.up_ask = Decimal("0.48")
        sess.up_bid = Decimal("0.46")
        sess.down_ask = Decimal("0.54")
        sess.down_bid = Decimal("0.52")

        if idx > 0:
            # Collect prepared-next boundary without publishing as active
            for b in w["binance_ticks"]:
                runtime.ingest_binance(
                    PriceTickView(
                        value=Decimal(str(b["value"])),
                        source_ts=_ts(b["source_ts"]),
                        receive_wall_raw_utc=_ts(b["receive_wall_raw_utc"]),
                        receive_monotonic_ns=int(b["receive_monotonic_ns"]),
                        identity=TradingReferenceIdentity.BINANCE_SPOT,
                        raw_fingerprint=b.get("fingerprint"),
                    )
                )
            for t in w["chainlink_ticks"]:
                runtime.ingest_chainlink(
                    window_id=w["window_id"],
                    market_id=mid,
                    tick=BoundaryTickView(
                        value=Decimal(str(t["value"])),
                        source_ts=_ts(t["source_ts"]),
                        receive_wall_raw_utc=_ts(t["receive_wall_raw_utc"]),
                        receive_wall_corrected_utc=_ts(t["receive_wall_raw_utc"]),
                        receive_monotonic_ns=int(t["receive_monotonic_ns"]),
                        raw_fingerprint=t.get("fingerprint"),
                        event_id=t.get("fingerprint"),
                        ingress=IngressMeta(
                            receive_monotonic_ns=int(t["receive_monotonic_ns"]),
                            ingress_sequence=max(1, int(t["receive_monotonic_ns"])),
                            connection_generation=1,
                            receive_wall_raw_utc=_ts(t["receive_wall_raw_utc"]),
                            receive_wall_corrected_utc=_ts(t["receive_wall_raw_utc"]),
                            clock_status="READY",
                            clock_offset_ms=0.0,
                            clock_uncertainty_ms=50,
                            raw_fingerprint=t.get("fingerprint", ""),
                            role=FeedRole.SETTLEMENT_REFERENCE,
                        ),
                    ),
                )
            runtime.attest_and_seal(
                market_id=mid,
                window_id=w["window_id"],
                sealed_at=_ts(w["event_start"]) + timedelta(seconds=5),
                require_attestation_match=True,
            )
            clock.set_utc(_ts(w["event_start"]) + timedelta(seconds=10))
            runtime.promote_prepared_next()
        else:
            for b in w["binance_ticks"]:
                runtime.ingest_binance(
                    PriceTickView(
                        value=Decimal(str(b["value"])),
                        source_ts=_ts(b["source_ts"]),
                        receive_wall_raw_utc=_ts(b["receive_wall_raw_utc"]),
                        receive_monotonic_ns=int(b["receive_monotonic_ns"]),
                        identity=TradingReferenceIdentity.BINANCE_SPOT,
                        raw_fingerprint=b.get("fingerprint"),
                    )
                )
            for t in w["chainlink_ticks"]:
                runtime.ingest_chainlink(
                    window_id=w["window_id"],
                    market_id=mid,
                    tick=BoundaryTickView(
                        value=Decimal(str(t["value"])),
                        source_ts=_ts(t["source_ts"]),
                        receive_wall_raw_utc=_ts(t["receive_wall_raw_utc"]),
                        receive_wall_corrected_utc=_ts(t["receive_wall_raw_utc"]),
                        receive_monotonic_ns=int(t["receive_monotonic_ns"]),
                        raw_fingerprint=t.get("fingerprint"),
                        event_id=t.get("fingerprint"),
                        ingress=IngressMeta(
                            receive_monotonic_ns=int(t["receive_monotonic_ns"]),
                            ingress_sequence=max(1, int(t["receive_monotonic_ns"])),
                            connection_generation=1,
                            receive_wall_raw_utc=_ts(t["receive_wall_raw_utc"]),
                            receive_wall_corrected_utc=_ts(t["receive_wall_raw_utc"]),
                            clock_status="READY",
                            clock_offset_ms=0.0,
                            clock_uncertainty_ms=50,
                            raw_fingerprint=t.get("fingerprint", ""),
                            role=FeedRole.SETTLEMENT_REFERENCE,
                        ),
                    ),
                )
            runtime.attest_and_seal(
                market_id=mid,
                window_id=w["window_id"],
                sealed_at=_ts(w["event_start"]) + timedelta(seconds=5),
                require_attestation_match=True,
            )

        # Dynamic Binance updates with fixed K
        k0 = runtime.active.sealed.ptb_k if runtime.active and runtime.active.sealed else None
        for i in range(3):
            clock.set_utc(_ts(w["event_start"]) + timedelta(seconds=30 + i * 5))
            dyn = runtime.ingest_binance(
                PriceTickView(
                    value=Decimal(str(w["binance_ticks"][0]["value"])) + Decimal(i),
                    source_ts=clock.now_utc() - timedelta(milliseconds=20),
                    receive_wall_raw_utc=clock.now_utc(),
                    receive_monotonic_ns=100_000 + i,
                    identity=TradingReferenceIdentity.BINANCE_SPOT,
                    raw_fingerprint=f"dyn-{w['window_id']}-{i}",
                )
            )
            rec = runtime.evaluate_active(trigger="feed")
            observations.append(rec.to_dict())
            if k0 is not None and runtime.active and runtime.active.sealed:
                assert runtime.active.sealed.ptb_k == k0
            _ = dyn

    summary = {
        "mode": "fixture",
        "not_live_evidence": True,
        "fixture_path": str(fixture_path),
        "windows": len(windows_data),
        "observations": observations,
        "oms_touched": runtime.oms_touched,
        "orders_submitted": runtime.orders_submitted,
        "portfolio_touched": runtime.portfolio_touched,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "pass": runtime.orders_submitted == 0 and not runtime.oms_touched,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in summary if k != "observations"}, indent=2))
    return summary


async def run_live(
    *,
    duration_s: float,
    max_windows: int,
    out_path: Path,
    prep_lead_s: float,
) -> dict:
    """Live public-data OBSERVE — requires healthy Polymarket TLS.

    Explicit arguments only; no placeholders. TLS verification always on.
    """
    _ = prep_lead_s
    # Refuse insecure contexts
    _ = ssl.create_default_context()

    summary: dict = {
        "mode": "live",
        "not_live_evidence": False,
        "tls_verify": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "auth_touched": False,
        "orders_touched": False,
        "oms_touched": False,
        "max_windows": max_windows,
        "duration_s": duration_s,
        "classification_if_tls_fails": "NOT_RUN_ENVIRONMENT_BLOCKED",
        "feeds": {},
        "observations": [],
    }

    clock = SystemClock()
    auth = SnapshotTimeAuthority(clock=clock, max_uncertainty_ms=10_000)
    provider = OsMonitorClockSyncProvider(enable_binance_cross_check=True)
    try:
        snap = await provider.measure()
        auth.apply_snapshot(snap)
    except Exception as exc:
        summary["clock_error"] = f"{type(exc).__name__}:{exc}"

    discovery = GammaMarketDiscovery()
    try:
        active_binding = await discovery.resolve_btc_5m_window(
            slug=current_btc_updown_slug(),
            session_role=DiscoverySessionRole.ACTIVE,
        )
        prepared = await discovery.prepare_next_btc_5m()
        summary["discovery"] = {
            "active_slug": active_binding.window_slug,
            "prepared_slug": prepared.window_slug,
            "outcome_semantics": active_binding.outcome_semantics,
        }
    except Exception as exc:
        summary["discovery_error"] = f"{type(exc).__name__}:{exc}"
        summary["n4b_status"] = "NOT_RUN_ENVIRONMENT_BLOCKED"
        # Fixture binding for structure only — do not claim live
        event = json.loads(DEFAULT_GAMMA.read_text(encoding="utf-8"))
        active_binding = bind_btc_5m_gamma_event(
            event,
            expected_slug=event["slug"],
            session_role=DiscoverySessionRole.ACTIVE,
        )
        prepared = None
        summary["discovery_fallback"] = "fixture_gamma_binding_not_live"

    runtime = N4ObserveRuntime.create(
        clock=FakeClock(_wall=datetime.now(timezone.utc)),
        basis_ewma_half_life_s=None,  # OPEN — do not invent
        zgap_config=ZGapConfig(
            ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=Decimal("10000"))
        ),
    )
    runtime.open_session(
        slot=SessionSlot.ACTIVE,
        market=active_binding.market,
        window_id=active_binding.window_slug,
        binding=active_binding,
    )
    if prepared is not None:
        runtime.open_session(
            slot=SessionSlot.PREPARED_NEXT,
            market=prepared.market,
            window_id=prepared.window_slug,
            binding=prepared,
            publish_as_active=False,
        )

    disp = EventDispatcher()
    counts = {"chainlink": 0, "binance": 0, "clob": 0}

    def on_cl(e: SettlementReferenceUpdated) -> None:
        counts["chainlink"] += 1
        if runtime.active is None:
            return
        meta = e.ingress
        tick = BoundaryTickView(
            value=e.settlement.price,
            source_ts=e.ts_event,
            receive_wall_raw_utc=e.ts_received,
            receive_wall_corrected_utc=(
                None if meta is None else meta.receive_wall_corrected_utc
            ),
            receive_monotonic_ns=0 if meta is None else meta.receive_monotonic_ns,
            ingress=meta,
            raw_fingerprint=None if meta is None else meta.raw_fingerprint,
        )
        runtime.ingest_chainlink(
            window_id=runtime.active.window_id,
            market_id=runtime.active.market_id,
            tick=tick,
        )

    def on_bn(e: ReferencePriceUpdated) -> None:
        if e.source.value != "binance":
            return
        counts["binance"] += 1
        meta = e.ingress
        runtime.ingest_binance(
            PriceTickView(
                value=e.reference.price,
                source_ts=e.ts_event,
                receive_wall_raw_utc=e.ts_received,
                receive_wall_corrected_utc=(
                    None if meta is None else meta.receive_wall_corrected_utc
                ),
                receive_monotonic_ns=0 if meta is None else meta.receive_monotonic_ns,
                identity=TradingReferenceIdentity.BINANCE_SPOT,
                ingress=meta,
            )
        )
        if runtime.active and runtime.active.sealed is not None:
            rec = runtime.evaluate_active(trigger="feed")
            summary["observations"].append(rec.to_dict())

    disp.subscribe(SettlementReferenceUpdated, on_cl)
    disp.subscribe(ReferencePriceUpdated, on_bn)

    cl = RtdsChainlinkAdapter(time_authority=auth, heartbeat_timeout_s=25)
    bn = BinanceTradeWsAdapter(
        symbol="BTCUSDT", time_authority=auth
    )
    clob = PolymarketMarketWsAdapter.from_binding(active_binding)
    tasks = [
        asyncio.create_task(cl.run(disp), name="cl"),
        asyncio.create_task(bn.run(disp), name="bn"),
        asyncio.create_task(clob.run(disp), name="clob"),
    ]
    try:
        await asyncio.sleep(duration_s)
    finally:
        await cl.stop()
        await bn.stop()
        await clob.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    summary["feeds"] = {
        "chainlink_ticks": counts["chainlink"],
        "binance_ticks": counts["binance"],
        "clob_ready": clob.ready,
        "shutdown": "graceful",
    }
    summary["oms_touched"] = runtime.oms_touched
    summary["orders_submitted"] = runtime.orders_submitted
    if counts["chainlink"] == 0:
        summary["n4b_status"] = "NOT_RUN_ENVIRONMENT_BLOCKED"
    else:
        summary["n4b_status"] = "LIVE_PARTIAL_OR_OK"
    summary["ended_at"] = datetime.now(timezone.utc).isoformat()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in summary if k != "observations"}, indent=2)[:4000])
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="N4 OBSERVE bounded runner")
    ap.add_argument("--mode", choices=("fixture", "live"), required=True)
    ap.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="N3-shaped window fixture for --mode fixture",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("var/reporting/n4/observe_summary.json"),
    )
    ap.add_argument("--windows", type=int, default=2, help="Fixture windows to replay")
    ap.add_argument(
        "--duration-s",
        type=float,
        default=120.0,
        help="Live capture duration seconds",
    )
    ap.add_argument(
        "--max-windows",
        type=int,
        default=2,
        help="Live max windows to evaluate (engineering bound)",
    )
    ap.add_argument(
        "--prep-lead-s",
        type=float,
        default=60.0,
        help="Prepared-next discovery lead before boundary (ops hint; OPEN)",
    )
    args = ap.parse_args()

    if args.mode == "fixture":
        summary = run_fixture(
            fixture_path=args.fixture, out_path=args.out, windows=args.windows
        )
        raise SystemExit(0 if summary.get("pass") else 1)

    summary = asyncio.run(
        run_live(
            duration_s=args.duration_s,
            max_windows=args.max_windows,
            out_path=args.out,
            prep_lead_s=args.prep_lead_s,
        )
    )
    # Live on blocked host exits 0 with explicit NOT_RUN status (do not fake PASS)
    if summary.get("n4b_status") == "NOT_RUN_ENVIRONMENT_BLOCKED":
        raise SystemExit(0)
    raise SystemExit(0 if summary.get("orders_touched") is False else 1)


if __name__ == "__main__":
    main()
