"""ENTRY vs STOP/URGENT_EXIT quality policy tests."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

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

TOKEN = TokenId("tok-ev")


def _gate(**kw) -> DataQualityGate:
    cfg = DataQualityGateConfig(
        enforcement_mode=EnforcementMode.ENFORCE.value,
        profiles={"crypto_5m": CRYPTO_5M_PROFILE},
        **kw,
    )
    return DataQualityGate(cfg)


def _snap(age_ms: int, *, source=BookSource.WEBSOCKET):
    store = MarketStateStore()
    ts = utc_now() - timedelta(milliseconds=age_ms)
    store.apply_book(
        TOKEN,
        [BookLevel(Decimal("0.48"), Decimal("100"))],
        [BookLevel(Decimal("0.52"), Decimal("100"))],
        source=source,
        received_ts=ts,
    )
    return store.capture(TOKEN, now=utc_now())


def test_entry_requires_pass_only() -> None:
    gate = _gate()
    for age in (500, 800, 2000):
        report = gate.evaluate_snapshot(_snap(age), context=DecisionContext.ENTRY, size=Decimal("5"))
        if age <= 750:
            assert report.verdict == QualityVerdict.PASS
            assert gate.allows_decision(report, DecisionContext.ENTRY)
        else:
            assert report.verdict == QualityVerdict.REJECT_DECISION
            assert not gate.allows_decision(report, DecisionContext.ENTRY)


def test_activation_requires_pass_only() -> None:
    gate = _gate()
    fresh = gate.evaluate_snapshot(_snap(500), context=DecisionContext.ACTIVATION, size=Decimal("5"))
    stale = gate.evaluate_snapshot(_snap(1760), context=DecisionContext.ACTIVATION, size=Decimal("5"))
    assert fresh.verdict == QualityVerdict.PASS
    assert stale.verdict == QualityVerdict.REJECT_DECISION
    assert not gate.allows_decision(stale, DecisionContext.ACTIVATION)


def test_timeout_urgent_exit_age_tiers() -> None:
    gate = _gate()
    pass_r = gate.evaluate_snapshot(_snap(500), context=DecisionContext.URGENT_EXIT, size=Decimal("5"))
    deg = gate.evaluate_snapshot(_snap(1200), context=DecisionContext.URGENT_EXIT, size=Decimal("5"))
    emerg = gate.evaluate_snapshot(_snap(2100), context=DecisionContext.URGENT_EXIT, size=Decimal("5"))
    reject = gate.evaluate_snapshot(_snap(3100), context=DecisionContext.URGENT_EXIT, size=Decimal("5"))
    assert pass_r.verdict == QualityVerdict.PASS
    assert deg.verdict == QualityVerdict.DEGRADED
    assert emerg.verdict == QualityVerdict.EMERGENCY_ONLY
    assert emerg.emergency_reason
    assert reject.verdict == QualityVerdict.REJECT_DECISION
    assert not gate.allows_decision(reject, DecisionContext.URGENT_EXIT)


def test_stop_allows_degraded_and_emergency() -> None:
    gate = _gate()
    degraded = gate.evaluate_snapshot(_snap(800), context=DecisionContext.STOP, size=Decimal("5"))
    emergency = gate.evaluate_snapshot(_snap(2000), context=DecisionContext.STOP, size=Decimal("5"))
    assert degraded.verdict == QualityVerdict.DEGRADED
    assert emergency.verdict == QualityVerdict.EMERGENCY_ONLY
    assert gate.allows_decision(degraded, DecisionContext.STOP)
    assert gate.allows_decision(emergency, DecisionContext.STOP)


def test_urgent_exit_same_as_stop() -> None:
    gate = _gate()
    report = gate.evaluate_snapshot(_snap(900), context=DecisionContext.URGENT_EXIT, size=Decimal("5"))
    assert report.verdict == QualityVerdict.DEGRADED
    assert gate.allows_decision(report, DecisionContext.URGENT_EXIT)


def test_rest_recovery_exit_allowed_when_configured() -> None:
    gate = _gate(allow_rest_recovery_for_exit=True)
    snap = _snap(500, source=BookSource.REST_RECOVERY)
    report = gate.evaluate_snapshot(snap, context=DecisionContext.STOP, size=Decimal("5"))
    assert report.verdict == QualityVerdict.PASS
    assert gate.allows_decision(report, DecisionContext.STOP)


def test_rest_recovery_never_for_entry() -> None:
    gate = _gate(allow_rest_recovery_for_exit=True)
    snap = _snap(500, source=BookSource.REST_RECOVERY)
    report = gate.evaluate_snapshot(snap, context=DecisionContext.ENTRY, size=Decimal("5"))
    assert report.verdict == QualityVerdict.REJECT_DECISION
