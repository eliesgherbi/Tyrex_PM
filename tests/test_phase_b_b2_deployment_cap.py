"""Phase B — portfolio / token deployment caps via :class:`~tyrex_pm.risk.configured.ConfiguredRiskPolicy`."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from tyrex_pm.config.loaders import (
    RiskSettings,
    load_runtime_settings,
    validate_phase_b_runtime_contract,
)
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent
from tyrex_pm.risk.configured import ConfiguredRiskPolicy


def _intent(
    *,
    qty: float = 1.0,
    price: float | None = 0.5,
    token: str = "t1",
) -> OrderIntent:
    return OrderIntent(
        correlation_id="c",
        token_id=token,
        side="BUY",
        quantity=qty,
        signal_kind="entry",
        reason_code="ok",
        price_ref=price,
    )


def _base_risk(**over) -> RiskSettings:
    r = RiskSettings(
        max_notional_usd_per_order=1_000.0,
        max_token_notional_usd_open=float("inf"),
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
    )
    return replace(r, **over) if over else r


def _make_db() -> MagicMock:
    m = MagicMock()
    m.portfolio_deployment_usd_with_policy.return_value = (99.0, True, None)
    m.token_deployment_usd_with_policy.return_value = (0.0, True, None)
    return m


def test_portfolio_cap_disabled_no_deployment_call_for_gate() -> None:
    s = _base_risk(max_portfolio_notional_usd_open=float("inf"))
    m = _make_db()
    pol = ConfiguredRiskPolicy(s, deployment_budget=m)
    ok, rc, _ = pol.evaluate(_intent())
    assert ok is True
    m.portfolio_deployment_usd_with_policy.assert_not_called()


def test_under_cap_allows() -> None:
    s = _base_risk(max_portfolio_notional_usd_open=100.0)
    m = MagicMock()
    m.portfolio_deployment_usd_with_policy.return_value = (30.0, True, None)
    m.token_deployment_usd_with_policy.return_value = (0.0, True, None)
    pol = ConfiguredRiskPolicy(s, deployment_budget=m)
    ok, rc, _ = pol.evaluate(_intent(qty=1.0, price=0.5))
    assert ok is True
    m.portfolio_deployment_usd_with_policy.assert_called()


def test_single_deployment_budget_api_for_portfolio_cap() -> None:
    """Portfolio cap uses only ``deployment_budget`` (no alternate sizing modes)."""
    s = _base_risk(max_portfolio_notional_usd_open=100.0)
    m = MagicMock(
        spec=["portfolio_deployment_usd_with_policy", "token_deployment_usd_with_policy"],
    )
    m.portfolio_deployment_usd_with_policy.return_value = (10.0, True, None)
    m.token_deployment_usd_with_policy.return_value = (0.0, True, None)
    pol = ConfiguredRiskPolicy(s, deployment_budget=m)
    pol.evaluate(_intent())
    m.portfolio_deployment_usd_with_policy.assert_called()


def test_ops_log_portfolio_cap_exceeded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="tyrex_pm.risk.configured")
    s = _base_risk(max_portfolio_notional_usd_open=30.0)
    m = MagicMock()
    m.portfolio_deployment_usd_with_policy.return_value = (29.0, True, None)
    m.token_deployment_usd_with_policy.return_value = (0.0, True, None)
    pol = ConfiguredRiskPolicy(s, deployment_budget=m)
    it = _intent(qty=10.0, price=0.5)
    ok, rc, _ = pol.evaluate(it)
    assert ok is False
    assert rc == ReasonCode.RISK_PORTFOLIO_DEPLOYMENT_EXCEEDED
    joined = " ".join(r.message for r in caplog.records)
    assert "tyrex_risk_ops" in joined
    assert "portfolio_deployment_cap" in joined


def test_ops_log_incomplete_portfolio_deployment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="tyrex_pm.risk.configured")
    s = _base_risk(
        max_portfolio_notional_usd_open=100.0,
        fail_on_unresolved_portfolio_deployment=True,
    )
    m = MagicMock()
    m.portfolio_deployment_usd_with_policy.return_value = (0.0, False, "boom")
    m.token_deployment_usd_with_policy.return_value = (0.0, True, None)
    pol = ConfiguredRiskPolicy(s, deployment_budget=m)
    it = _intent()
    ok, rc, _ = pol.evaluate(it)
    assert ok is False
    assert rc == ReasonCode.RISK_PORTFOLIO_DEPLOYMENT_UNRESOLVED


def test_ops_log_no_deployment_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="tyrex_pm.risk.configured")
    s = _base_risk(max_portfolio_notional_usd_open=1.0)
    pol = ConfiguredRiskPolicy(s, deployment_budget=None)
    it = _intent()
    ok, rc, _ = pol.evaluate(it)
    assert ok is False
    joined = " ".join(r.message for r in caplog.records)
    assert "no_reader" in joined or "portfolio_deployment" in joined


def test_over_cap_denies() -> None:
    s = _base_risk(max_portfolio_notional_usd_open=30.0)
    m = MagicMock()
    m.portfolio_deployment_usd_with_policy.return_value = (29.0, True, None)
    m.token_deployment_usd_with_policy.return_value = (0.0, True, None)
    pol = ConfiguredRiskPolicy(s, deployment_budget=m)
    ok, rc, _ = pol.evaluate(_intent(qty=10.0, price=0.5))
    assert ok is False
    assert rc == ReasonCode.RISK_PORTFOLIO_DEPLOYMENT_EXCEEDED


def test_lenient_portfolio_filled_uses_pending_only_in_eval() -> None:
    s = _base_risk(
        max_portfolio_notional_usd_open=100.0,
        fail_on_unresolved_portfolio_deployment=False,
    )
    m = MagicMock()
    m.portfolio_deployment_usd_with_policy.return_value = (80.0, True, None)
    m.token_deployment_usd_with_policy.return_value = (0.0, True, None)
    pol = ConfiguredRiskPolicy(s, deployment_budget=m)
    ok, rc, _ = pol.evaluate(_intent(qty=10.0, price=2.0))
    assert ok is True
    call_kw = m.portfolio_deployment_usd_with_policy.call_args.kwargs
    assert call_kw["strict_filled"] is False


def test_order_deploy_exceeds_per_order_cap_when_deny_policy() -> None:
    s = _base_risk(max_notional_usd_per_order=1.0, max_notional_policy="deny")
    pol = ConfiguredRiskPolicy(s, deployment_budget=_make_db())
    ok, rc, _ = pol.evaluate(_intent(qty=10.0, price=0.5))
    assert ok is False
    assert rc == ReasonCode.RISK_ORDER_DEPLOYMENT_EXCEEDED


def test_order_deploy_clips_to_per_order_cap_when_cap_policy() -> None:
    s = _base_risk(max_notional_usd_per_order=1.0, max_notional_policy="cap")
    pol = ConfiguredRiskPolicy(s, deployment_budget=_make_db())
    ok, rc, intent = pol.evaluate(_intent(qty=10.0, price=0.5))
    assert ok is True
    assert rc == "approved"
    assert intent is not None
    assert intent.quantity == pytest.approx(2.0)


def test_missing_price_denies_when_portfolio_cap_needs_n() -> None:
    s = replace(
        _base_risk(max_portfolio_notional_usd_open=50.0),
        fail_on_missing_price_for_notional=False,
    )
    m = MagicMock()
    m.portfolio_deployment_usd_with_policy.return_value = (10.0, True, None)
    m.token_deployment_usd_with_policy.return_value = (0.0, True, None)
    pol = ConfiguredRiskPolicy(s, deployment_budget=m)
    ok, rc, _ = pol.evaluate(_intent(price=None))
    assert ok is False
    assert rc == ReasonCode.RISK_MISSING_PRICE


def test_no_deployment_budget_with_finite_cap_denies() -> None:
    s = _base_risk(max_portfolio_notional_usd_open=1.0)
    pol = ConfiguredRiskPolicy(s, deployment_budget=None)
    ok, rc, _ = pol.evaluate(_intent())
    assert ok is False
    assert rc == ReasonCode.RISK_PORTFOLIO_DEPLOYMENT_UNRESOLVED


def test_b0_yaml_with_obsolete_framework_submit_key_rejected(tmp_path: Path) -> None:
    rt_path = tmp_path / "rt.yaml"
    rt_path.write_text(
        yaml.safe_dump(
            {
                "trader_id": "T-001",
                "execution_mode": "live",
                "polymarket_framework_submit": False,
                "polymarket_instrument_ids": ["0xabc-1.POLYMARKET"],
            },
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="obsolete"):
        load_runtime_settings(rt_path)


def test_cap_uses_portfolio_deploy_plus_n_not_double_pending() -> None:
    s = _base_risk(max_portfolio_notional_usd_open=100.0)
    m = MagicMock()
    m.portfolio_deployment_usd_with_policy.return_value = (60.0, True, None)
    m.token_deployment_usd_with_policy.return_value = (0.0, True, None)
    pol = ConfiguredRiskPolicy(s, deployment_budget=m)
    ok, rc, _ = pol.evaluate(_intent(qty=10.0, price=5.0))
    assert ok is False
    assert rc == ReasonCode.RISK_PORTFOLIO_DEPLOYMENT_EXCEEDED
