"""DataQualityGate unit tests (Phase 2 M3)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.market_data.models import BookLevel, BookSource, SourceQuality
from tyrex_pm.market_data.quality import (
    CRYPTO_5M_PROFILE,
    DataQualityGate,
    DataQualityGateConfig,
    DecisionContext,
    EnforcementMode,
    QualityVerdict,
)
from tyrex_pm.state.market_store import MarketStateStore

TOKEN = TokenId("tok-q")


def _gate(*, enforce: bool = True) -> DataQualityGate:
    return DataQualityGate(
        DataQualityGateConfig(
            enforcement_mode=EnforcementMode.ENFORCE.value if enforce else EnforcementMode.OBSERVE_ONLY.value,
            market_profile="crypto_5m",
            profiles={"crypto_5m": CRYPTO_5M_PROFILE},
        )
    )


def _snap(*, age_ms: int, source=BookSource.WEBSOCKET, sq: str | None = None):
    store = MarketStateStore()
    ts = utc_now() - timedelta(milliseconds=age_ms)
    store.apply_book(
        TOKEN,
        [BookLevel(Decimal("0.48"), Decimal("100"))],
        [BookLevel(Decimal("0.52"), Decimal("100"))],
        source=source,
        source_quality=sq or (SourceQuality.WS_PRIMARY if source == BookSource.WEBSOCKET else None),
        received_ts=ts,
    )
    return store.capture(TOKEN, now=utc_now())


def test_pass_at_500ms_ws_primary_entry() -> None:
    gate = _gate()
    report = gate.evaluate_snapshot(_snap(age_ms=500), context=DecisionContext.ENTRY, size=Decimal("10"))
    assert report.verdict == QualityVerdict.PASS
    assert gate.allows_decision(report, DecisionContext.ENTRY)


def test_entry_800ms_reject_stop_800ms_degraded() -> None:
    gate = _gate()
    snap = _snap(age_ms=800)
    entry = gate.evaluate_snapshot(snap, context=DecisionContext.ENTRY, size=Decimal("10"))
    stop = gate.evaluate_snapshot(snap, context=DecisionContext.STOP, size=Decimal("10"))
    assert entry.verdict == QualityVerdict.REJECT_DECISION
    assert stop.verdict == QualityVerdict.DEGRADED
    assert gate.allows_decision(stop, DecisionContext.STOP)


def test_entry_2000ms_reject_stop_emergency_only() -> None:
    gate = _gate()
    snap = _snap(age_ms=2000)
    entry = gate.evaluate_snapshot(snap, context=DecisionContext.ENTRY, size=Decimal("10"))
    stop = gate.evaluate_snapshot(snap, context=DecisionContext.STOP, size=Decimal("10"))
    assert entry.verdict == QualityVerdict.REJECT_DECISION
    assert stop.verdict == QualityVerdict.EMERGENCY_ONLY
    assert stop.emergency_reason is not None


def test_age_3500ms_reject_all_contexts() -> None:
    gate = _gate()
    snap = _snap(age_ms=3500)
    for ctx in DecisionContext:
        report = gate.evaluate_snapshot(snap, context=ctx, size=Decimal("10"))
        assert report.verdict == QualityVerdict.REJECT_DECISION


def test_tp_at_degraded_age_rejected() -> None:
    gate = _gate()
    snap = _snap(age_ms=900)
    report = gate.evaluate_snapshot(snap, context=DecisionContext.TAKE_PROFIT, size=Decimal("10"))
    assert report.verdict == QualityVerdict.REJECT_DECISION


def test_rest_bootstrap_entry_rejected_in_enforce() -> None:
    gate = _gate()
    snap = _snap(age_ms=100, source=BookSource.REST_BOOTSTRAP, sq=SourceQuality.REST_BOOTSTRAP)
    report = gate.evaluate_snapshot(snap, context=DecisionContext.ENTRY, size=Decimal("10"))
    assert report.verdict == QualityVerdict.REJECT_DECISION
    assert "source_not_ws_primary" in report.reasons or "rest_bootstrap" in str(report.reasons)


def test_observe_only_never_blocks() -> None:
    gate = _gate(enforce=False)
    snap = _snap(age_ms=3500)
    report = gate.evaluate_snapshot(snap, context=DecisionContext.ENTRY, size=Decimal("10"))
    assert report.verdict == QualityVerdict.REJECT_DECISION
    assert gate.allows_decision(report, DecisionContext.ENTRY)


def test_reconnect_gap_blocks_entry() -> None:
    gate = _gate()
    store = MarketStateStore()
    ts = utc_now()
    store.apply_book(
        TOKEN,
        [BookLevel(Decimal("0.48"), Decimal("100"))],
        [BookLevel(Decimal("0.52"), Decimal("100"))],
        source=BookSource.WEBSOCKET,
        received_ts=ts,
        reconnect_gap=True,
    )
    snap = store.capture(TOKEN, now=ts)
    report = gate.evaluate_snapshot(snap, context=DecisionContext.ENTRY, size=Decimal("10"))
    assert report.verdict == QualityVerdict.REJECT_DECISION
    assert "reconnect_gap" in report.reasons
