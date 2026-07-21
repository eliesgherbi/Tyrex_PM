#!/usr/bin/env python3
"""Run the offline deterministic N5A SHADOW harness.

Only ``--mode fixture`` is implemented.  N5B live operation is intentionally
deferred: it would require separately approved public-data ingress and remains
outside this command; this script never contacts live endpoints.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import MarketId, TokenId
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot
from tyrex_pm.domain.polymarket.boundary_candidates import BoundaryTickView
from tyrex_pm.domain.polymarket.market import (
    BinaryMarket,
    MarketStatus,
    make_binary_instruments,
)
from tyrex_pm.domain.polymarket.ptb_attestation import FixturePtbAttestationProvider
from tyrex_pm.indicators.causal_pairing import PriceTickView, TradingReferenceIdentity
from tyrex_pm.runtime.config import SourceMode, load_observe_config
from tyrex_pm.runtime.n4_observe_runtime import SessionSlot
from tyrex_pm.runtime.n5_shadow_runtime import N5ShadowRuntime

REPO = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO / "config" / "observe_shadow_z_gap_n5a.json"


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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="N5A offline deterministic Z-Gap SHADOW configuration check"
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
    summary = {
        "mode": "fixture",
        "n5a": True,
        "not_live": True,
        "not_live_evidence": True,
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
        "records": [record.to_dict() for record in runtime.records],
        "orders_submitted": runtime.orders_submitted,
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
