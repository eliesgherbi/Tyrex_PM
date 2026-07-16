"""Indicator, signal, and fact envelopes."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tyrex_pm.core.facts import FactEnvelope
from tyrex_pm.core.ids import CorrelationId, EventId, RunId, StrategyId
from tyrex_pm.core.indicators import IndicatorResult
from tyrex_pm.core.signals import Signal


UTC = timezone.utc


def test_indicator_and_signal_envelopes() -> None:
    ind = IndicatorResult(
        indicator_id="momentum",
        observed_at=datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC),
        value=Decimal("0.01"),
        source_event_id=EventId("e1"),
    )
    assert ind.value == Decimal("0.01")
    sig = Signal(
        signal_type="directional",
        observed_at=datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC),
        correlation_id=CorrelationId("c1"),
        causation_id=EventId("e1"),
        evidence={"strength": "0.7"},
    )
    assert sig.evidence["strength"] == "0.7"
    with pytest.raises(Exception):
        sig.signal_type = "x"  # type: ignore[misc]


def test_fact_envelope() -> None:
    fact = FactEnvelope(
        fact_type="signal_emitted",
        schema_version=1,
        ts=datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC),
        run_id=RunId("run-1"),
        correlation_id=CorrelationId("c1"),
        strategy_id=StrategyId("framework_validation"),
        payload={"ok": True},
    )
    assert fact.schema_version == 1
    with pytest.raises(ValueError):
        FactEnvelope(
            fact_type="x",
            schema_version=0,
            ts=datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC),
            run_id=RunId("run-1"),
            correlation_id=CorrelationId("c1"),
        )
