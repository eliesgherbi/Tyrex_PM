"""EMERGENCY_ONLY semantics — exits only, never entry/TP."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.market_data.models import BookLevel, BookSource
from tyrex_pm.market_data.quality import (
    CRYPTO_5M_PROFILE,
    DataQualityGate,
    DataQualityGateConfig,
    DecisionContext,
    EnforcementMode,
    QualityVerdict,
)
from tyrex_pm.state.market_store import MarketStateStore

TOKEN = TokenId("tok-em")


def _gate() -> DataQualityGate:
    return DataQualityGate(
        DataQualityGateConfig(
            enforcement_mode=EnforcementMode.ENFORCE.value,
            profiles={"crypto_5m": CRYPTO_5M_PROFILE},
        )
    )


def _snap(age_ms: int):
    store = MarketStateStore()
    ts = utc_now() - timedelta(milliseconds=age_ms)
    store.apply_book(
        TOKEN,
        [BookLevel(Decimal("0.48"), Decimal("100"))],
        [BookLevel(Decimal("0.52"), Decimal("100"))],
        source=BookSource.WEBSOCKET,
        received_ts=ts,
    )
    return store.capture(TOKEN, now=utc_now())


@pytest.mark.parametrize(
    "context",
    [DecisionContext.ENTRY, DecisionContext.TAKE_PROFIT],
)
def test_emergency_never_for_entry_or_tp(context: DecisionContext) -> None:
    gate = _gate()
    report = gate.evaluate_snapshot(_snap(2100), context=context, size=Decimal("10"))
    assert report.verdict == QualityVerdict.REJECT_DECISION
    assert not gate.allows_decision(report, context)


def test_emergency_only_for_stop_includes_reason() -> None:
    gate = _gate()
    report = gate.evaluate_snapshot(_snap(2100), context=DecisionContext.STOP, size=Decimal("10"))
    assert report.verdict == QualityVerdict.EMERGENCY_ONLY
    assert report.emergency_reason is not None
    assert gate.allows_decision(report, DecisionContext.STOP)


def test_emergency_only_not_pass() -> None:
    gate = _gate()
    report = gate.evaluate_snapshot(_snap(2500), context=DecisionContext.URGENT_EXIT, size=Decimal("10"))
    assert report.verdict == QualityVerdict.EMERGENCY_ONLY
    assert report.verdict != QualityVerdict.PASS
