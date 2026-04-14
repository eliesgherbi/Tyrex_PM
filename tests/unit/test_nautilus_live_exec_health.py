"""WP2 — :class:`~tyrex_pm.runtime.tradable_state.nautilus_live_health.NautilusLiveExecutionHealthSource`."""

from __future__ import annotations

import asyncio

import pytest

from tyrex_pm.config.loaders import RiskSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent
from tyrex_pm.risk.configured import ConfiguredRiskPolicy
from tyrex_pm.runtime.tradable_state import (
    NautilusLiveExecutionHealthSource,
    TradableStateHealth,
)
from tyrex_pm.runtime.tradable_state.stub import UnknownBootstrapHealthSource


class _FakeLiveExecEngine:
    __slots__ = ("_startup_reconciliation_event",)

    def __init__(self) -> None:
        self._startup_reconciliation_event = asyncio.Event()


def test_constructor_rejects_missing_event() -> None:
    with pytest.raises(TypeError, match="asyncio.Event"):
        NautilusLiveExecutionHealthSource(object())


def test_snapshot_pending_vs_complete() -> None:
    eng = _FakeLiveExecEngine()
    src = NautilusLiveExecutionHealthSource(eng)
    s0 = src.snapshot()
    assert s0.level == TradableStateHealth.UNKNOWN_BOOTSTRAP
    assert "pending" in s0.reason_code
    eng._startup_reconciliation_event.set()
    s1 = src.snapshot()
    assert s1.level == TradableStateHealth.HEALTHY
    assert "complete" in s1.reason_code
    assert s1.framework_detail is not None


def test_risk_allows_when_event_set() -> None:
    eng = _FakeLiveExecEngine()
    eng._startup_reconciliation_event.set()
    pol = ConfiguredRiskPolicy(
        RiskSettings(
            max_notional_usd_per_order=100.0,
            max_token_notional_usd_open=float("inf"),
            kill_switch=False,
            fail_on_missing_price_for_notional=True,
            tradable_state_health_gate_enabled=True,
        ),
        tradable_state_health_source=NautilusLiveExecutionHealthSource(eng),
    )
    ok, rc, _ = pol.evaluate(
        OrderIntent(
            correlation_id="x",
            token_id="t",
            side="BUY",
            quantity=1.0,
            signal_kind="entry",
            reason_code="ok",
            price_ref=0.5,
        ),
    )
    assert ok and rc == "approved"


def test_risk_denies_when_event_pending() -> None:
    eng = _FakeLiveExecEngine()
    pol = ConfiguredRiskPolicy(
        RiskSettings(
            max_notional_usd_per_order=100.0,
            max_token_notional_usd_open=float("inf"),
            kill_switch=False,
            fail_on_missing_price_for_notional=True,
            tradable_state_health_gate_enabled=True,
        ),
        tradable_state_health_source=NautilusLiveExecutionHealthSource(eng),
    )
    ok, rc, _ = pol.evaluate(
        OrderIntent(
            correlation_id="x",
            token_id="t",
            side="BUY",
            quantity=1.0,
            signal_kind="entry",
            reason_code="ok",
            price_ref=0.5,
        ),
    )
    assert not ok
    assert rc == str(ReasonCode.RISK_HEALTH_UNKNOWN_BOOTSTRAP)


def test_placeholder_unknown_bootstrap_differs_from_live_source_pending() -> None:
    eng = _FakeLiveExecEngine()
    live_snap = NautilusLiveExecutionHealthSource(eng).snapshot()
    stub_snap = UnknownBootstrapHealthSource().snapshot()
    assert live_snap.reason_code != stub_snap.reason_code
    assert live_snap.level == stub_snap.level == TradableStateHealth.UNKNOWN_BOOTSTRAP