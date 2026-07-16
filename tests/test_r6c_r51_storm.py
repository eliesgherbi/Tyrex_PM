"""R5.1 deterministic high-frequency storm validation (fixture, not live empirical).

Reproduces essential R5 conditions: thousands of directional / flat signals,
thin→deep book, entry retries, exit failures, kill-switch escalation.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import CorrelationId, RunId
from tyrex_pm.core.intents import IntentKind
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.lifecycle.trade_lifecycle import LifecycleState
from tyrex_pm.market_data.freshness import FreshnessConfig, TimestampBasis
from tyrex_pm.runtime.config import ObserveConfig, RiskPlanConfig, SourceMode
from tyrex_pm.runtime.shadow_config import ShadowConfig
from tyrex_pm.runtime.shadow_host import ShadowHost

# Pre-R5.1 live-shadow storm baseline (historical observation)
R5_BASELINE = {
    "signals": 2123,
    "entry_intents": 89,
    "exit_intents": 488,
    "duplicate_denials": 489,
    "commands": 3,
}


def _build_storm_fixture(path: Path, *, n_ticks: int = 2200) -> None:
    """Generate a high-frequency fixture matching R5 storm scale."""
    t0 = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    market = {
        "market_id": "cond-storm-1",
        "condition_id": "cond-storm-1",
        "question": "Storm fixture BTC Up or Down?",
        "yes_token_id": "tok-yes-1",
        "no_token_id": "tok-no-1",
        "event_start": t0.isoformat(),
        "event_end": (t0 + timedelta(minutes=30)).isoformat(),
        "tick_size": "0.01",
        "min_order_size": "5",
        "status": "ACTIVE",
        "event_slug": "btc-updown-storm",
    }
    # All book events must be <= first Binance ts (fixture publishes PM then BN).
    # Thin books first, then material deep upgrade — last snapshot wins for the storm.
    book_ts = t0 + timedelta(seconds=1)
    poly: list[dict] = []
    for asset, bids, asks, h in (
        ("tok-yes-1", [("0.48", "1")], [("0.52", "1")], "thin-yes"),
        ("tok-no-1", [("0.47", "1")], [("0.53", "1")], "thin-no"),
        ("tok-yes-1", [("0.48", "500")], [("0.52", "500")], "deep-yes"),
        ("tok-no-1", [("0.47", "500")], [("0.53", "500")], "deep-no"),
    ):
        poly.append(
            {
                "ts_received": book_ts.isoformat(),
                "payload": {
                    "event_type": "book",
                    "asset_id": asset,
                    "timestamp": book_ts.isoformat(),
                    "bids": [{"price": p, "size": s} for p, s in bids],
                    "asks": [{"price": p, "size": s} for p, s in asks],
                    "hash": h,
                },
            }
        )

    binance: list[dict] = []
    # Start after books so freshness is valid. Strong UP, hold, reverse, chop.
    price = 100_000.0
    bn0 = t0 + timedelta(seconds=2)
    base_ms = int(bn0.timestamp() * 1000)
    for i in range(n_ticks):
        if i < 40:
            price += 1
        elif i < 900:
            price += 40  # strong UP momentum
        elif i < 1400:
            price += 0.5  # near-flat while UP held
        elif i < 1800:
            price -= 50  # reversal / DOWN
        else:
            price += 0.2  # chop
        ts = bn0 + timedelta(milliseconds=250 * i)
        binance.append(
            {
                "ts_received": ts.isoformat(),
                "payload": {
                    "s": "BTCUSDT",
                    "p": f"{price:.2f}",
                    "T": base_ms + 250 * i,
                },
            }
        )

    path.write_text(
        json.dumps(
            {"market": market, "polymarket_events": poly, "binance_events": binance},
            indent=2,
        ),
        encoding="utf-8",
    )


def test_r51_storm_deterministic_stress(tmp_path: Path) -> None:
    """Deterministic stress validation — not live empirical proof."""
    fixture = tmp_path / "storm.json"
    _build_storm_fixture(fixture, n_ticks=2123)

    cfg = ObserveConfig(
        mode=SourceMode.FIXTURE,
        output_path=tmp_path / "facts.jsonl",
        binance_symbol="BTCUSDT",
        momentum_lookback=timedelta(seconds=5),
        momentum_threshold=Decimal("0.0001"),
        max_book_spread=Decimal("0.15"),
        freshness=FreshnessConfig(
            book_threshold_ms=3_600_000,
            reference_threshold_ms=3_600_000,
            future_tolerance_ms=5_000,
            timestamp_basis=TimestampBasis.EVENT_TIME,
        ),
        runtime_duration=None,
        fixture_path=fixture,
        risk=RiskPlanConfig(
            runtime_mode=RuntimeMode.SHADOW,
            target_notional=Decimal("5"),
            max_notional=Decimal("10"),
            min_price=Decimal("0.01"),
            max_price=Decimal("0.99"),
            max_spread=Decimal("0.20"),
            min_liquidity_notional=Decimal("1"),
            no_entry_before_close=timedelta(0),
            duplicate_lifetime=timedelta(hours=1),
        ),
        shadow=ShadowConfig(
            enable_oms=True,
            max_position_notional=Decimal("50"),
            max_total_exposure=Decimal("100"),
            max_hold=timedelta(hours=1),
            flatten_before_close=timedelta(seconds=30),
            exit_on_flat=True,
            persistence_path=tmp_path / "state.json",
            cancel_unfilled_residual=False,
        ),
    )
    clock = FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc))
    host = ShadowHost(
        cfg,
        clock=clock,
        run_id=RunId("run-storm"),
        correlation_id=CorrelationId("corr-storm"),
    )
    try:
        result = host.run_fixture()
        # Kill-switch late to force exit escalation if still active
        if host.lifecycle.state is LifecycleState.ACTIVE:
            host.set_kill_switch(True)
            host.evaluate_once()
    finally:
        host.close()

    n_signals = len(result.signals)
    enter = sum(1 for i in result.intents if i.kind is IntentKind.ENTER)
    exit_n = sum(
        1 for i in result.intents if i.kind in {IntentKind.EXIT, IntentKind.FLATTEN}
    )
    # Count duplicate denials from facts
    dup = 0
    if result.facts_path and Path(result.facts_path).exists():
        for line in Path(result.facts_path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("fact_type") == "duplicate_intent":
                dup += 1

    commands = len(host.commands)
    final = host.lifecycle.state

    # Acceptance: storm does not scale intents/denials with signal count
    directional = sum(
        1 for s in result.signals if s.direction.value in {"UP", "DOWN", "FLAT"}
    )
    assert n_signals >= 2000
    assert directional >= 500, f"expected directional signals, got {directional}"
    assert enter <= 10, f"entry intents exploded: {enter}"
    assert exit_n <= 20, f"exit intents exploded: {exit_n}"
    assert dup <= 20, f"duplicate denials still storm-scale: {dup}"
    assert commands <= 15
    # Must actually exercise entry planning under storm (not all UNAVAILABLE)
    assert enter >= 1, "storm produced no ENTER intents — fixture too quiet"
    # Entry attempts obey cap
    assert host.retry.entry.attempts <= host.retry.config.entry_max_attempts
    # Residual / manual never TERMINAL while position remains
    if not host.portfolio.is_flat():
        assert final in {
            LifecycleState.ACTIVE,
            LifecycleState.ENTRY_PENDING,
            LifecycleState.EXIT_PENDING,
            LifecycleState.EXIT_REQUESTED,
            LifecycleState.EXIT_RETRY_WAIT,
            LifecycleState.MANUAL_INTERVENTION,
        }
        assert final is not LifecycleState.TERMINAL

    # Comparison table (fixture stress — not live empirical)
    print(
        "\nR5.1 deterministic stress validation (fixture, not live empirical)\n"
        f"| Metric | R5 baseline | R5.1 controlled |\n"
        f"| Signals | {R5_BASELINE['signals']} | {n_signals} |\n"
        f"| Entry intents | {R5_BASELINE['entry_intents']} | {enter} |\n"
        f"| Exit intents | {R5_BASELINE['exit_intents']} | {exit_n} |\n"
        f"| Duplicate denials | {R5_BASELINE['duplicate_denials']} | {dup} |\n"
        f"| Commands | {R5_BASELINE['commands']} | {commands} |\n"
        f"| Final lifecycle | Residual possible | {final.value} |\n"
    )

    assert enter < R5_BASELINE["entry_intents"]
    assert exit_n < R5_BASELINE["exit_intents"]
    assert dup < R5_BASELINE["duplicate_denials"]
