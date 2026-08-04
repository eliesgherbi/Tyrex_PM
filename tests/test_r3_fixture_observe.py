"""Deterministic fixture-driven vertical slice."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import CorrelationId, RunId
from tyrex_pm.market_data.freshness import FreshnessConfig, TimestampBasis
from tyrex_pm.reporting import open_run_reporter
from tyrex_pm.runtime.config import ObserveConfig, SourceMode, observe_config_from_mapping
from tyrex_pm.runtime.observe_host import ObserveHost
from tyrex_pm.signals.directional import Direction
from tyrex_pm.strategies.framework_validation.reference_momentum import ObserveDecisionKind

from helpers_reporting import legacy_fact_types, load_run_events

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "r3_observe_complete.json"
FIXTURE_DOWN = ROOT / "tests" / "fixtures" / "r3_observe_down.json"


def _cfg(tmp_path: Path, fixture: Path) -> ObserveConfig:
    return ObserveConfig(
        mode=SourceMode.FIXTURE,
        output_path=tmp_path / "facts.jsonl",
        binance_symbol="BTCUSDT",
        momentum_lookback=timedelta(seconds=5),
        momentum_threshold=Decimal("0.001"),
        max_book_spread=Decimal("0.10"),
        freshness=FreshnessConfig(
            book_threshold_ms=60_000,
            reference_threshold_ms=60_000,
            future_tolerance_ms=500,
            timestamp_basis=TimestampBasis.EVENT_TIME,
        ),
        runtime_duration=None,
        fixture_path=fixture,
    )


def test_complete_chain_up(tmp_path: Path) -> None:
    clock = FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc))
    host = ObserveHost(
        _cfg(tmp_path, FIXTURE),
        clock=clock,
        run_id=RunId("run-r3a"),
        correlation_id=CorrelationId("corr-r3a"),
    )
    try:
        result = host.run_fixture()
    finally:
        host.close()

    assert result.market.yes.token_id.value == "tok-yes-1"
    assert any(
        d.action.value == "ENTER"
        and d.evidence.get("validation_kind") == ObserveDecisionKind.WOULD_ENTER_UP.value
        for d in result.decisions
    )
    assert any(s.direction is Direction.UP for s in result.signals)
    events = load_run_events(result.run_dir or result.facts_path.parent)
    types = legacy_fact_types(events)
    assert "market_resolved" in types
    assert "observe_decision" in types
    assert "signal" in types
    for row in events:
        assert row.get("correlation_id") == "corr-r3a" or row.get("run_id") == "run-r3a"
        assert row["run_id"] == "run-r3a"


def test_deterministic_semantic_output(tmp_path: Path) -> None:
    def run_once(path: Path) -> list[str]:
        clock = FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc))
        host = ObserveHost(
            _cfg(path, FIXTURE),
            clock=clock,
            run_id=RunId("run-det"),
            correlation_id=CorrelationId("corr-det"),
        )
        try:
            host.run_fixture()
            kinds = [
                d.evidence.get("validation_kind", d.action.value) for d in host.decisions
            ]
            dirs = [s.direction.value for s in host.signals]
            return kinds + dirs
        finally:
            host.close()

    a = run_once(tmp_path / "a")
    b = run_once(tmp_path / "b")
    assert a == b


def test_down_fixture(tmp_path: Path) -> None:
    clock = FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc))
    host = ObserveHost(_cfg(tmp_path, FIXTURE_DOWN), clock=clock)
    try:
        result = host.run_fixture()
    finally:
        host.close()
    assert any(
        d.action.value == "ENTER"
        and d.evidence.get("validation_kind") == ObserveDecisionKind.WOULD_ENTER_DOWN.value
        for d in result.decisions
    )


def test_reporter_jsonl_roundtrip(tmp_path: Path) -> None:
    rep = open_run_reporter(
        run_dir=tmp_path / "r3_rep",
        run_id="r",
        mode="observe",
        strategy_id="reference_momentum",
    )
    evt = rep.emit_dict(
        event_family="lifecycle",
        event_type="test_fact",
        payload={"price": "0.5"},
        producer="test_r3",
        force_critical=True,
    )
    rep.finalize()
    lines = (tmp_path / "r3_rep" / "audit_events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    loaded = json.loads(lines[-1])
    assert loaded["event_type"] == "test_fact"
    assert loaded["payload"]["price"] == "0.5"
    assert loaded["event_id"] == evt.event_id


def test_config_validation_requires_thresholds() -> None:
    try:
        observe_config_from_mapping(
            {
                "mode": "fixture",
                "fixture_path": "x.json",
                "output_path": "out.jsonl",
                "momentum_lookback_ms": 1000,
                "momentum_threshold": "0.01",
                "max_book_spread": "0.1",
                "freshness": {},
            }
        )
        raised = False
    except ValueError:
        raised = True
    assert raised
