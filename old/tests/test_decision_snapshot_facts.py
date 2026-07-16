"""Decision snapshot fact builder tests."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.market_data.decision_snapshot import build_decision_snapshot
from tyrex_pm.market_data.executable_book import ExecutableBookView, build_planner_evidence
from tyrex_pm.market_data.models import BookLevel, BookSource
from tyrex_pm.market_data.quality import (
    CRYPTO_5M_PROFILE,
    DataQualityGate,
    DataQualityGateConfig,
    DecisionContext,
)
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_DECISION_SNAPSHOT, validate_fact_payload
from tyrex_pm.state.market_store import MarketStateStore

TOKEN = TokenId("tok-ds")


def test_features_snapshot_id_matches_planner() -> None:
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
    gate = DataQualityGate(DataQualityGateConfig(profiles={"crypto_5m": CRYPTO_5M_PROFILE}))
    from tyrex_pm.core.enums import Side

    report = gate.evaluate_snapshot(snap, context=DecisionContext.ENTRY, size=Decimal("10"))
    view = ExecutableBookView.from_snapshot(snap, side=Side.BUY, size=Decimal("10"))
    pe = build_planner_evidence(decision_id="dec-1", snap=snap, view=view, quality_report=report)
    decision = build_decision_snapshot(
        decision_type="entry",
        snap=snap,
        quality_report=report,
        planner_evidence=pe,
        size=Decimal("10"),
        decision_id="dec-1",
    )
    payload = decision.to_payload()
    assert payload["features"]["snapshot_id"] == pe.snapshot_id
    missing = validate_fact_payload(FACT_TYPE_DECISION_SNAPSHOT, payload)
    assert missing == []


def test_pair_snapshot_includes_both_legs() -> None:
    store = MarketStateStore()
    yes = TokenId("yes-ds")
    no = TokenId("no-ds")
    for tok in (yes, no):
        store.apply_book(
            tok,
            [BookLevel(Decimal("0.48"), Decimal("100"))],
            [BookLevel(Decimal("0.52"), Decimal("100"))],
            source=BookSource.WEBSOCKET,
            received_ts=utc_now(),
        )
    pair = store.capture_pair(yes, no, "pair-ds", now=utc_now())
    assert pair is not None
    gate = DataQualityGate(DataQualityGateConfig(profiles={"crypto_5m": CRYPTO_5M_PROFILE}))
    report = gate.evaluate_pair(pair, context=DecisionContext.ENTRY, size=Decimal("5"))
    decision = build_decision_snapshot(
        decision_type="entry_eval",
        pair=pair,
        quality_report=report,
        size=Decimal("5"),
        decision_id="dec-pair",
    )
    assert len(decision.snapshot_ids) == 2
    assert decision.pair_snapshot_id == pair.pair_snapshot_id
