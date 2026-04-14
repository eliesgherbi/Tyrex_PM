"""Phase 2 — TradableStateHealth matrix in :class:`~tyrex_pm.risk.configured.ConfiguredRiskPolicy`."""

from __future__ import annotations

from datetime import UTC, datetime

from tyrex_pm.config.loaders import RiskSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent
from tyrex_pm.risk.configured import ConfiguredRiskPolicy
from tyrex_pm.runtime.tradable_state import (
    StaticTradableStateHealthSource,
    TradableStateHealth,
    TradableStateHealthSnapshot,
    tradable_health_allows_intent,
)


def _risk_tsh(**over: object) -> RiskSettings:
    base: dict[str, object] = {
        "max_notional_usd_per_order": 1000.0,
        "max_token_notional_usd_open": float("inf"),
        "kill_switch": False,
        "fail_on_missing_price_for_notional": True,
        "tradable_state_health_gate_enabled": True,
        "allow_exit_when_degraded_oms": False,
    }
    base.update(over)
    return RiskSettings(**base)  # type: ignore[arg-type]


def _snap(level: TradableStateHealth) -> TradableStateHealthSnapshot:
    return TradableStateHealthSnapshot(
        level=level,
        reason_code="test",
        observed_at_utc=datetime.now(tz=UTC),
        framework_detail=None,
    )


def _intent(side: str = "BUY") -> OrderIntent:
    return OrderIntent(
        correlation_id="h1",
        token_id="88888",
        side=side,
        quantity=1.0,
        signal_kind="entry",
        reason_code="ok",
        price_ref=0.5,
    )


def test_matrix_healthy_allows_buy_and_sell() -> None:
    s = _snap(TradableStateHealth.HEALTHY)
    assert tradable_health_allows_intent(s, side_upper="BUY", allow_exit_when_degraded_oms=False)[0]
    assert tradable_health_allows_intent(s, side_upper="SELL", allow_exit_when_degraded_oms=False)[0]


def test_matrix_unknown_denies() -> None:
    s = _snap(TradableStateHealth.UNKNOWN_BOOTSTRAP)
    assert not tradable_health_allows_intent(s, side_upper="BUY", allow_exit_when_degraded_oms=False)[0]
    assert not tradable_health_allows_intent(s, side_upper="SELL", allow_exit_when_degraded_oms=False)[0]


def test_matrix_divergent_denies() -> None:
    s = _snap(TradableStateHealth.DIVERGENT_PERSISTENT)
    assert not tradable_health_allows_intent(s, side_upper="BUY", allow_exit_when_degraded_oms=False)[0]
    assert not tradable_health_allows_intent(s, side_upper="SELL", allow_exit_when_degraded_oms=True)[0]


def test_matrix_degraded_buy_denies_sell_flag_sensitive() -> None:
    s = _snap(TradableStateHealth.DEGRADED_OMS)
    assert not tradable_health_allows_intent(s, side_upper="BUY", allow_exit_when_degraded_oms=True)[0]
    assert not tradable_health_allows_intent(s, side_upper="SELL", allow_exit_when_degraded_oms=False)[0]
    ok, _ = tradable_health_allows_intent(s, side_upper="SELL", allow_exit_when_degraded_oms=True)
    assert ok


def test_risk_evaluate_gate_off_skips_health() -> None:
    pol = ConfiguredRiskPolicy(
        RiskSettings(
            max_notional_usd_per_order=1000.0,
            max_token_notional_usd_open=float("inf"),
            kill_switch=False,
            fail_on_missing_price_for_notional=True,
            tradable_state_health_gate_enabled=False,
        ),
    )
    ok, rc, _ = pol.evaluate(_intent())
    assert ok and rc == "approved"


def test_risk_evaluate_healthy_allows() -> None:
    pol = ConfiguredRiskPolicy(
        _risk_tsh(),
        tradable_state_health_source=StaticTradableStateHealthSource(_snap(TradableStateHealth.HEALTHY)),
    )
    ok, rc, _ = pol.evaluate(_intent())
    assert ok and rc == "approved"


def test_risk_evaluate_unknown_denies() -> None:
    pol = ConfiguredRiskPolicy(
        _risk_tsh(),
        tradable_state_health_source=StaticTradableStateHealthSource(
            _snap(TradableStateHealth.UNKNOWN_BOOTSTRAP),
        ),
    )
    ok, rc, _ = pol.evaluate(_intent())
    assert not ok
    assert rc == str(ReasonCode.RISK_HEALTH_UNKNOWN_BOOTSTRAP)


def test_risk_gate_enabled_missing_source_fail_closed() -> None:
    pol = ConfiguredRiskPolicy(_risk_tsh(), tradable_state_health_source=None)
    ok, rc, _ = pol.evaluate(_intent())
    assert not ok
    assert rc == str(ReasonCode.RISK_HEALTH_UNKNOWN_BOOTSTRAP)


def test_wp4_missing_health_source_emits_joinable_tradable_state_health_fact() -> None:
    rows: list[tuple[str, dict]] = []

    def fe(ft: str, pl: dict) -> None:
        rows.append((ft, pl))

    pol = ConfiguredRiskPolicy(_risk_tsh(), tradable_state_health_source=None, fact_emit=fe)
    ok, rc, _ = pol.evaluate(_intent())
    assert not ok
    assert rc == str(ReasonCode.RISK_HEALTH_UNKNOWN_BOOTSTRAP)
    hrows = [p for t, p in rows if t == "tradable_state_health"]
    assert len(hrows) == 1
    h = hrows[0]
    assert h["level"] == "unknown_bootstrap"
    assert h["reason_code"] == "health_source_missing"
    assert h["risk_allowed"] is False
    assert h["risk_reason_code"] == str(ReasonCode.RISK_HEALTH_UNKNOWN_BOOTSTRAP)
    assert h["reporting_only_synthetic"] is True
    rd = next(p for t, p in rows if t == "risk_decision")
    assert rd["tradable_state_health_level"] == "unknown_bootstrap"
    assert rd["tradable_state_health_reason_code"] == "health_source_missing"


def test_tradable_state_health_fact_emitted_when_reporting() -> None:
    rows: list[tuple[str, dict]] = []

    def fe(ft: str, pl: dict) -> None:
        rows.append((ft, pl))

    pol = ConfiguredRiskPolicy(
        _risk_tsh(),
        tradable_state_health_source=StaticTradableStateHealthSource(_snap(TradableStateHealth.HEALTHY)),
        fact_emit=fe,
    )
    pol.evaluate(_intent())
    types = [t for t, _ in rows]
    assert "tradable_state_health" in types
    h = next(p for t, p in rows if t == "tradable_state_health")
    assert h["level"] == "healthy"
    assert h["correlation_id"] == "h1"
    assert "observed_at_utc" in h
    assert h["risk_allowed"] is True
