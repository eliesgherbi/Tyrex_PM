"""R4 end-to-end fixture: signal → intent → risk → plan → facts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import CorrelationId, RunId
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.market_data.freshness import FreshnessConfig, TimestampBasis
from tyrex_pm.planning.plan import PlanStatus
from tyrex_pm.risk.reasons import RiskReason
from tyrex_pm.runtime.config import ObserveConfig, RiskPlanConfig, SourceMode
from tyrex_pm.runtime.observe_host import ObserveHost
from tyrex_pm.signals.directional import Direction
from tyrex_pm.strategies.framework_validation.reference_momentum import ObserveDecisionKind

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "r3_observe_complete.json"


def _risk(**overrides) -> RiskPlanConfig:
    base = dict(
        runtime_mode=RuntimeMode.SHADOW,
        target_notional=Decimal("5"),
        max_notional=Decimal("10"),
        min_price=Decimal("0.01"),
        max_price=Decimal("0.99"),
        max_spread=Decimal("0.20"),
        min_liquidity_notional=Decimal("1"),
        no_entry_before_close=timedelta(seconds=0),
        duplicate_lifetime=timedelta(hours=1),
        kill_switch_active=False,
    )
    base.update(overrides)
    return RiskPlanConfig(**base)


def _cfg(tmp_path: Path, risk: RiskPlanConfig) -> ObserveConfig:
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
        fixture_path=FIXTURE,
        risk=risk,
    )


def test_e2e_up_intent_approved_planned(tmp_path: Path) -> None:
    clock = FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc))
    host = ObserveHost(
        _cfg(tmp_path, _risk()),
        clock=clock,
        run_id=RunId("run-r4"),
        correlation_id=CorrelationId("corr-r4"),
    )
    try:
        result = host.run_fixture()
    finally:
        host.close()

    assert any(s.direction is Direction.UP for s in result.signals)
    assert any(
        d.action.value == "ENTER"
        and d.evidence.get("validation_kind") == ObserveDecisionKind.WOULD_ENTER_UP.value
        for d in result.decisions
    )
    assert len(result.intents) == 1
    assert result.intents[0].target_notional == Decimal("5")
    assert any(r.approved for r in result.risk_decisions)
    assert any(p.status is PlanStatus.PLANNED for p in result.plans)
    types = {
        json.loads(line)["fact_type"]
        for line in result.facts_path.read_text(encoding="utf-8").splitlines()
    }
    assert "intent_created" in types
    assert "risk_approved" in types
    assert "execution_plan_created" in types


def test_e2e_kill_switch_denies(tmp_path: Path) -> None:
    host = ObserveHost(
        _cfg(tmp_path, _risk(kill_switch_active=True)),
        clock=FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)),
    )
    try:
        result = host.run_fixture()
    finally:
        host.close()
    assert result.intents
    assert all(not r.approved for r in result.risk_decisions)
    assert any(RiskReason.KILL_SWITCH_ACTIVE in r.reason_codes for r in result.risk_decisions)
    assert result.plans == []


def test_e2e_live_tiny_denied(tmp_path: Path) -> None:
    host = ObserveHost(
        _cfg(tmp_path, _risk(runtime_mode=RuntimeMode.LIVE_TINY)),
        clock=FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)),
    )
    try:
        result = host.run_fixture()
    finally:
        host.close()
    assert any(RiskReason.LIVE_NOT_SUPPORTED in r.reason_codes for r in result.risk_decisions)
    assert result.plans == []


def test_r3_signal_path_without_risk_unchanged(tmp_path: Path) -> None:
    """Without risk config, no intents — same observe decisions as R3."""
    cfg = ObserveConfig(
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
        fixture_path=FIXTURE,
        risk=None,
    )
    host = ObserveHost(
        cfg,
        clock=FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)),
    )
    try:
        result = host.run_fixture()
    finally:
        host.close()
    assert result.intents == []
    assert result.risk_decisions == []
    assert any(
        d.action.value == "ENTER"
        and d.evidence.get("validation_kind") == ObserveDecisionKind.WOULD_ENTER_UP.value
        for d in result.decisions
    )
