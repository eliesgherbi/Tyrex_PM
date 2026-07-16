"""PlannerEvidence emission tests."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId
from tyrex_pm.core.models import ApprovedIntent, ExitIntent, URGENCY_URGENT
from tyrex_pm.core.time import utc_now
from tyrex_pm.execution.planner import ExecutionPlanner
from tyrex_pm.market_data.executable_book import ExecutableBookView, build_planner_evidence
from tyrex_pm.market_data.models import BookLevel, BookSource
from tyrex_pm.market_data.quality import (
    CRYPTO_5M_PROFILE,
    DataQualityGate,
    DataQualityGateConfig,
    DecisionContext,
    QualityVerdict,
)
from tyrex_pm.runtime.config import ExecutionPlannerConfig
from tyrex_pm.state.market_store import MarketStateStore

TOKEN = TokenId("tok-pe")


def _store(*, age_ms: int = 100):
    store = MarketStateStore()
    ts = utc_now() - timedelta(milliseconds=age_ms)
    store.apply_book(
        TOKEN,
        [BookLevel(Decimal("0.49"), Decimal("100"))],
        [BookLevel(Decimal("0.51"), Decimal("100"))],
        source=BookSource.WEBSOCKET,
        received_ts=ts,
    )
    return store


def _urgent_approved(size=Decimal("50")) -> ApprovedIntent:
    intent = ExitIntent(
        token_id=TOKEN,
        side=Side.SELL,
        size=size,
        limit_price=Decimal("0.4"),
        order_style=OrderStyle.FAK,
        urgency=URGENCY_URGENT,
    )
    return ApprovedIntent(intent=intent, client_order_id=ClientOrderId("c1"), run_id=RunId("r1"))


def test_planner_evidence_in_result() -> None:
    store = _store()
    snap = store.capture(TOKEN, now=utc_now())
    assert snap is not None
    gate = DataQualityGate(DataQualityGateConfig(profiles={"crypto_5m": CRYPTO_5M_PROFILE}))
    report = gate.evaluate_snapshot(snap, context=DecisionContext.URGENT_EXIT, size=Decimal("50"))
    view = ExecutableBookView.from_snapshot(snap, side=Side.SELL, size=Decimal("50"))
    evidence = build_planner_evidence(
        decision_id="d1", snap=snap, view=view, quality_report=report
    )
    planner = ExecutionPlanner(
        ExecutionPlannerConfig(enabled=True, use_executable_depth=True),
        quality_gate=gate,
    )
    result = planner.plan(
        _urgent_approved(),
        market_state=store,
        now=utc_now(),
        captured_snapshot=snap,
        executable_view=view,
        quality_report=report,
        planner_evidence=evidence,
        decision_context=DecisionContext.URGENT_EXIT,
    )
    assert result.approved
    assert result.evidence is not None
    assert result.evidence["planner_evidence"]["snapshot_id"] == snap.snapshot_id
    assert result.evidence["planner_evidence"]["quality_verdict"] == QualityVerdict.PASS.value


def test_degraded_evidence_carries_verdict() -> None:
    store = _store(age_ms=900)
    snap = store.capture(TOKEN, now=utc_now())
    assert snap is not None
    gate = DataQualityGate(DataQualityGateConfig(profiles={"crypto_5m": CRYPTO_5M_PROFILE}))
    report = gate.evaluate_snapshot(snap, context=DecisionContext.STOP, size=Decimal("50"))
    assert report.verdict == QualityVerdict.DEGRADED
    view = ExecutableBookView.from_snapshot(snap, side=Side.SELL, size=Decimal("50"))
    evidence = build_planner_evidence(
        decision_id="d2", snap=snap, view=view, quality_report=report
    )
    assert evidence.quality_verdict == QualityVerdict.DEGRADED.value


def test_emergency_evidence_includes_reason() -> None:
    store = _store(age_ms=2100)
    snap = store.capture(TOKEN, now=utc_now())
    assert snap is not None
    gate = DataQualityGate(DataQualityGateConfig(profiles={"crypto_5m": CRYPTO_5M_PROFILE}))
    report = gate.evaluate_snapshot(snap, context=DecisionContext.URGENT_EXIT, size=Decimal("50"))
    assert report.verdict == QualityVerdict.EMERGENCY_ONLY
    view = ExecutableBookView.from_snapshot(snap, side=Side.SELL, size=Decimal("50"))
    evidence = build_planner_evidence(
        decision_id="d3", snap=snap, view=view, quality_report=report
    )
    assert evidence.emergency_reason is not None
