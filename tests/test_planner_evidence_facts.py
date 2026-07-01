"""Planner evidence fact schema validation."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.market_data.executable_book import ExecutableBookView, PlannerEvidence, build_planner_evidence
from tyrex_pm.market_data.models import BookLevel, BookSource
from tyrex_pm.market_data.quality import (
    CRYPTO_5M_PROFILE,
    DataQualityGate,
    DataQualityGateConfig,
    DecisionContext,
    QualityVerdict,
)
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_EXECUTION_PLANNER_EVIDENCE, validate_fact_payload
from tyrex_pm.state.market_store import MarketStateStore

TOKEN = TokenId("tok-pef")


def test_schema_validates_planner_evidence() -> None:
    store = MarketStateStore()
    store.apply_book(
        TOKEN,
        [BookLevel(Decimal("0.49"), Decimal("100"))],
        [BookLevel(Decimal("0.51"), Decimal("100"))],
        source=BookSource.WEBSOCKET,
        received_ts=utc_now(),
    )
    snap = store.capture(TOKEN, now=utc_now())
    assert snap is not None
    from tyrex_pm.core.enums import Side

    gate = DataQualityGate(DataQualityGateConfig(profiles={"crypto_5m": CRYPTO_5M_PROFILE}))
    report = gate.evaluate_snapshot(snap, context=DecisionContext.STOP, size=Decimal("10"))
    view = ExecutableBookView.from_snapshot(snap, side=Side.SELL, size=Decimal("10"))
    evidence = build_planner_evidence(
        decision_id="d-ev", snap=snap, view=view, quality_report=report
    )
    payload = evidence.to_payload()
    assert validate_fact_payload(FACT_TYPE_EXECUTION_PLANNER_EVIDENCE, payload) == []


def test_distinct_decision_ids_per_retry() -> None:
    e1 = PlannerEvidence(
        decision_id="a",
        snapshot_id="s1",
        book_age_ms=100,
        source=BookSource.WEBSOCKET,
        touch_price=Decimal("0.5"),
        worst_price_to_fill=Decimal("0.5"),
        sweep_vwap=Decimal("0.5"),
        expected_slippage=Decimal("0"),
        available_depth=Decimal("10"),
        quality_verdict=QualityVerdict.PASS.value,
    )
    e2 = PlannerEvidence(
        decision_id="b",
        snapshot_id="s2",
        book_age_ms=200,
        source=BookSource.WEBSOCKET,
        touch_price=Decimal("0.5"),
        worst_price_to_fill=Decimal("0.48"),
        sweep_vwap=Decimal("0.49"),
        expected_slippage=Decimal("0.01"),
        available_depth=Decimal("10"),
        quality_verdict=QualityVerdict.DEGRADED.value,
    )
    assert e1.decision_id != e2.decision_id
    assert e1.snapshot_id != e2.snapshot_id
