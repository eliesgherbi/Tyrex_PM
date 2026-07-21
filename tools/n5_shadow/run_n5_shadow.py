#!/usr/bin/env python3
"""Run the offline deterministic N5A SHADOW harness.

Only ``--mode fixture`` is implemented.  N5B live operation is intentionally
deferred: it would require separately approved public-data ingress and remains
outside this command; this script never contacts live endpoints.

The summary includes:
1. N5 composition smoke (N4 seal + evaluate_shadow labels)
2. A complete synthetic F4 entry→exit lifecycle through ShadowHost using
   ``shadow_depth_walk_v1`` (fixture-only; not live evidence)
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import CorrelationId, MarketId, RunId, TokenId
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot
from tyrex_pm.domain.polymarket.boundary_candidates import BoundaryTickView
from tyrex_pm.domain.polymarket.market import (
    BinaryMarket,
    MarketStatus,
    make_binary_instruments,
)
from tyrex_pm.domain.polymarket.ptb_attestation import FixturePtbAttestationProvider
from tyrex_pm.execution.shadow_fill_model import FILL_MODEL_DEPTH_WALK_V1
from tyrex_pm.indicators.causal_pairing import PriceTickView, TradingReferenceIdentity
from tyrex_pm.runtime.config import SourceMode, load_observe_config, observe_config_from_mapping
from tyrex_pm.runtime.n4_observe_runtime import SessionSlot
from tyrex_pm.runtime.n5_shadow_runtime import N5ShadowRuntime
from tyrex_pm.runtime.shadow_host import ShadowHost

REPO = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO / "config" / "observe_shadow_z_gap_n5a.json"
F4_CONFIG = REPO / "config" / "observe_shadow_z_gap_f4.json"


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _run_first_window(config) -> N5ShadowRuntime:
    """Small bounded N3-shaped replay; this is intentionally not a live adapter."""
    assert config.fixture_path is not None
    fixture_path = config.fixture_path
    if not fixture_path.is_absolute():
        fixture_path = REPO / fixture_path
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    window = data["windows"][0]
    start = _ts(window["event_start"])
    clock = FakeClock(_wall=start)
    runtime = N5ShadowRuntime.create(
        config,
        clock=clock,
        attestation_port=FixturePtbAttestationProvider(
            by_window={
                window["window_id"]: (
                    window["attested_open_price"],
                    window["attestation_source"],
                    {"fixture": True, "not_live": True},
                )
            }
        ),
        basis_ewma_half_life_s=30.0,
    )
    market_id = MarketId(window["market_id"])
    yes, no = make_binary_instruments(
        market_id=market_id,
        yes_token=TokenId(f"up-{window['window_id']}"),
        no_token=TokenId(f"down-{window['window_id']}"),
    )
    market = BinaryMarket(
        market_id=market_id,
        condition_id=window["window_id"],
        question=f"N5 fixture {window['window_id']}",
        yes=yes,
        no=no,
        event_start=start,
        event_end=_ts(window["event_end"]),
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
        status=MarketStatus.ACTIVE,
    )
    runtime.open_session(slot=SessionSlot.ACTIVE, market=market, window_id=window["window_id"])
    for item in window["binance_ticks"]:
        runtime.ingest_binance(
            PriceTickView(
                value=Decimal(str(item["value"])),
                source_ts=_ts(item["source_ts"]),
                receive_wall_raw_utc=_ts(item["receive_wall_raw_utc"]),
                receive_monotonic_ns=int(item["receive_monotonic_ns"]),
                identity=TradingReferenceIdentity.BINANCE_SPOT,
                raw_fingerprint=item.get("fingerprint"),
            )
        )
    for item in window["chainlink_ticks"]:
        runtime.ingest_chainlink(
            window_id=window["window_id"],
            market_id=market_id,
            tick=BoundaryTickView(
                value=Decimal(str(item["value"])),
                source_ts=_ts(item["source_ts"]),
                receive_wall_raw_utc=_ts(item["receive_wall_raw_utc"]),
                receive_wall_corrected_utc=_ts(item["receive_wall_raw_utc"]),
                receive_monotonic_ns=int(item["receive_monotonic_ns"]),
                raw_fingerprint=item.get("fingerprint"),
            ),
        )
    runtime.attest_and_seal(
        market_id=market_id,
        window_id=window["window_id"],
        sealed_at=start + timedelta(seconds=5),
        require_attestation_match=True,
    )
    clock.set_utc(start + timedelta(seconds=30))
    for instrument, bid, ask in ((yes.instrument_id, "0.46", "0.48"), (no.instrument_id, "0.52", "0.54")):
        runtime.publish_book(
            BookSnapshot(
                instrument_id=instrument,
                ts_event=clock.now_utc(),
                bids=(BookLevel(Decimal(bid), Decimal("100")),),
                asks=(BookLevel(Decimal(ask), Decimal("100")),),
            ),
            available_at=clock.now_utc(),
        )
    runtime.evaluate_shadow(trigger="feed")
    return runtime


def _run_depth_walk_lifecycle(out_dir: Path) -> dict:
    """Complete synthetic entry→exit via ShadowHost + shadow_depth_walk_v1.

    Uses the F4 rich-exit timeline with fixture-only fill parameters. This is
    synthetic fixture evidence — not recorded or live market evidence.
    """
    raw = json.loads(F4_CONFIG.read_text(encoding="utf-8"))
    raw["output_path"] = str(out_dir / "lifecycle_facts.jsonl")
    raw["fixture_path"] = str(REPO / raw["fixture_path"])
    raw["shadow"]["persistence_path"] = str(out_dir / "lifecycle_state.json")
    raw["shadow"]["fills"] = {
        "model_id": FILL_MODEL_DEPTH_WALK_V1,
        "latency_ms": 0,
        "extra_slip_ticks": "0",
        "tick_size": "0.01",
    }
    cfg = observe_config_from_mapping(raw)
    clock = FakeClock(_wall=datetime(2026, 7, 20, 12, 1, 0, tzinfo=timezone.utc))
    host = ShadowHost(
        cfg,
        clock=clock,
        run_id=RunId("n5a-fixture-lifecycle"),
        correlation_id=CorrelationId("n5a-fixture-lifecycle"),
    )
    try:
        result = host.run_fixture()
        traces = []
        if host.oms is not None:
            for t in host.oms.match_traces:
                traces.append(
                    {
                        "fill_model_id": t.fill_model_id,
                        "outcome": t.outcome,
                        "filled_qty": str(t.filled_qty),
                        "residual_qty": str(t.residual_qty),
                        "latency_ms": t.latency_ms,
                        "decision_plan_time": t.decision_plan_time.isoformat(),
                        "simulated_arrival": t.simulated_arrival.isoformat(),
                        "selected_book_available_at": None
                        if t.selected_book_available_at is None
                        else t.selected_book_available_at.isoformat(),
                        "selected_book_ts_event": None
                        if t.selected_book_ts_event is None
                        else t.selected_book_ts_event.isoformat(),
                        "extra_slip_ticks": str(t.extra_slip_ticks),
                    }
                )
        orders = []
        for o in host.order_store._orders.values():
            orders.append(
                {
                    "side": o.side.value,
                    "status": o.status.value,
                    "quantity": str(o.quantity),
                    "filled_quantity": str(o.filled_quantity),
                    "limit_price": str(o.limit_price),
                    "instrument_id": o.instrument_id.value,
                }
            )
        return {
            "evidence_class": "synthetic_fixture",
            "not_live_evidence": True,
            "fill_model_id": FILL_MODEL_DEPTH_WALK_V1,
            "windows_exercised": [str(result.market.market_id.value)],
            "evaluation_attempts": len(result.decisions),
            "decisions": [d.action.value for d in result.decisions],
            "intent_types": [type(i).__name__ for i in result.intents],
            "orders": orders,
            "match_traces": traces,
            "lifecycle_terminal": host.lifecycle.state.value,
            "portfolio_flat": host.portfolio.is_flat(),
            "fills_count": len(host.fills_ledger.all_fills()),
            "economics_label": "simulated_shadow",
            "fees_label": "estimated",
            "orders_live": 0,
            "live_oms": False,
        }
    finally:
        host.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="N5A offline deterministic Z-Gap SHADOW fixture harness"
    )
    parser.add_argument("--mode", choices=("fixture",), default="fixture")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO / "var" / "reporting" / "n5" / "shadow_summary.json",
    )
    args = parser.parse_args()

    config = load_observe_config(args.config)
    if config.mode is not SourceMode.FIXTURE:
        raise SystemExit("N5A requires a fixture-mode ObserveConfig")
    runtime = _run_first_window(config)
    lifecycle = _run_depth_walk_lifecycle(args.out.parent)
    summary = {
        "mode": "fixture",
        "n5a": True,
        "not_live": True,
        "not_live_evidence": True,
        "evidence_class": "synthetic_fixture",
        "live_oms": False,
        "orders_live": 0,
        "config": str(args.config),
        "fixture_path": None if config.fixture_path is None else str(config.fixture_path),
        "fill_model_id": config.shadow.fill_model_id if config.shadow else None,
        "latency_ms": config.shadow.fill_latency_ms if config.shadow else None,
        "economics_label": "simulated_shadow",
        "fees_label": "estimated",
        "resolution_capability": bool(
            config.z_gap and config.z_gap.resolution_capability
        ),
        "n5_composition_records": [record.to_dict() for record in runtime.records],
        "orders_submitted_composition": runtime.orders_submitted,
        "depth_walk_lifecycle": lifecycle,
        "n5b_status": "NOT_RUN_ENVIRONMENT_BLOCKED",
        "deferred_n5b_command": (
            "python tools/n5_shadow/run_n5_shadow_live.py --mode live "
            "--duration-s 180 --max-windows 2 "
            "--out var/reporting/n5/shadow_live_summary.json"
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
