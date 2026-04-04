"""Phase B B2 — portfolio-wide notional cap via B1 aggregator (unit tests)."""

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
from tyrex_pm.runtime.portfolio_exposure import PortfolioExposureAggregate


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
        max_order_quantity=100.0,
        max_notional_usd_per_order=1_000.0,
        max_token_notional_usd_open=float("inf"),
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
    )
    return replace(r, **over) if over else r


def _make_agg(aggregate: PortfolioExposureAggregate) -> MagicMock:
    m = MagicMock()
    m.aggregate.return_value = aggregate
    return m


def test_portfolio_cap_disabled_no_aggregator_call() -> None:
    s = _base_risk(max_portfolio_notional_usd_open=float("inf"))
    agg = _make_agg(
        PortfolioExposureAggregate(
            complete=True,
            pending_notional_usd=9.0,
            pending_complete=True,
            filled_net_exposure_usd=0.0,
            filled_complete=True,
            e_portfolio=99.0,
            error=None,
            omitted_instruments_unresolved_mark=(),
        ),
    )
    pol = ConfiguredRiskPolicy(s, portfolio_exposure=agg)
    ok, rc = pol.evaluate(_intent())
    assert ok is True
    agg.aggregate.assert_not_called()


def test_under_cap_allows() -> None:
    s = _base_risk(max_portfolio_notional_usd_open=100.0)
    agg = _make_agg(
        PortfolioExposureAggregate(
            complete=True,
            pending_notional_usd=10.0,
            pending_complete=True,
            filled_net_exposure_usd=-20.0,
            filled_complete=True,
            e_portfolio=10.0 + abs(-20.0),
            error=None,
            omitted_instruments_unresolved_mark=(),
        ),
    )
    pol = ConfiguredRiskPolicy(s, portfolio_exposure=agg)
    ok, rc = pol.evaluate(_intent(qty=1.0, price=0.5))
    assert ok is True
    agg.aggregate.assert_called_once()
    call_kw = agg.aggregate.call_args.kwargs
    assert call_kw["fail_on_unresolved"] == s.fail_on_unresolved_portfolio_exposure


def test_ops_log_portfolio_cap_exceeded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="tyrex_pm.risk.configured")
    s = _base_risk(max_portfolio_notional_usd_open=30.0)
    agg = _make_agg(
        PortfolioExposureAggregate(
            complete=True,
            pending_notional_usd=0.0,
            pending_complete=True,
            filled_net_exposure_usd=0.0,
            filled_complete=True,
            e_portfolio=29.0,
            error=None,
            omitted_instruments_unresolved_mark=(),
        ),
    )
    pol = ConfiguredRiskPolicy(s, portfolio_exposure=agg)
    it = _intent(qty=10.0, price=0.5)
    ok, rc = pol.evaluate(it)
    assert ok is False
    assert rc == ReasonCode.RISK_PORTFOLIO_NOTIONAL_CAP_EXCEEDED
    joined = " ".join(r.message for r in caplog.records)
    assert "tyrex_risk_ops" in joined
    assert "gate=portfolio_cap" in joined
    assert "e_portfolio=29" in joined or "e_portfolio=29.0" in joined
    assert it.correlation_id in joined


def test_ops_log_incomplete_b1(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="tyrex_pm.risk.configured")
    s = _base_risk(
        max_portfolio_notional_usd_open=100.0,
        fail_on_unresolved_portfolio_exposure=True,
    )
    agg = _make_agg(
        PortfolioExposureAggregate(
            complete=False,
            pending_notional_usd=0.0,
            pending_complete=False,
            filled_net_exposure_usd=0.0,
            filled_complete=True,
            e_portfolio=None,
            error="filled: unresolved mark for non-flat instrument",
            omitted_instruments_unresolved_mark=(),
        ),
    )
    pol = ConfiguredRiskPolicy(s, portfolio_exposure=agg)
    it = _intent()
    ok, rc = pol.evaluate(it)
    assert ok is False
    assert rc == ReasonCode.RISK_PORTFOLIO_EXPOSURE_UNRESOLVED
    joined = " ".join(r.message for r in caplog.records)
    assert "tyrex_risk_ops" in joined
    assert "gate=portfolio_unresolved" in joined
    assert "b1_pending_complete=False" in joined
    assert "b1_filled_complete=True" in joined
    assert "unresolved mark" in joined
    assert it.correlation_id in joined


def test_ops_log_no_b1_aggregator(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="tyrex_pm.risk.configured")
    s = _base_risk(max_portfolio_notional_usd_open=1.0)
    pol = ConfiguredRiskPolicy(s, portfolio_exposure=None)
    it = _intent()
    ok, rc = pol.evaluate(it)
    assert ok is False
    joined = " ".join(r.message for r in caplog.records)
    assert "tyrex_risk_ops" in joined
    assert "no_b1_aggregator" in joined
    assert it.correlation_id in joined


def test_over_cap_denies() -> None:
    s = _base_risk(max_portfolio_notional_usd_open=30.0)
    agg = _make_agg(
        PortfolioExposureAggregate(
            complete=True,
            pending_notional_usd=0.0,
            pending_complete=True,
            filled_net_exposure_usd=0.0,
            filled_complete=True,
            e_portfolio=29.0,
            error=None,
            omitted_instruments_unresolved_mark=(),
        ),
    )
    pol = ConfiguredRiskPolicy(s, portfolio_exposure=agg)
    ok, rc = pol.evaluate(_intent(qty=10.0, price=0.5))
    assert ok is False
    assert rc == ReasonCode.RISK_PORTFOLIO_NOTIONAL_CAP_EXCEEDED


def test_strict_incomplete_denies_unresolved() -> None:
    s = _base_risk(
        max_portfolio_notional_usd_open=100.0,
        fail_on_unresolved_portfolio_exposure=True,
    )
    agg = _make_agg(
        PortfolioExposureAggregate(
            complete=False,
            pending_notional_usd=0.0,
            pending_complete=False,
            filled_net_exposure_usd=0.0,
            filled_complete=False,
            e_portfolio=None,
            error="pending: boom",
            omitted_instruments_unresolved_mark=(),
        ),
    )
    pol = ConfiguredRiskPolicy(s, portfolio_exposure=agg)
    ok, rc = pol.evaluate(_intent())
    assert ok is False
    assert rc == ReasonCode.RISK_PORTFOLIO_EXPOSURE_UNRESOLVED


def test_unsafe_incomplete_still_denies_unresolved() -> None:
    """Unsafe mode does not skip the portfolio gate when B1 cannot produce a valid scalar."""
    s = _base_risk(
        max_portfolio_notional_usd_open=10.0,
        fail_on_unresolved_portfolio_exposure=False,
    )
    agg = _make_agg(
        PortfolioExposureAggregate(
            complete=False,
            pending_notional_usd=0.0,
            pending_complete=False,
            filled_net_exposure_usd=0.0,
            filled_complete=False,
            e_portfolio=None,
            error="pending: broken",
            omitted_instruments_unresolved_mark=(),
        ),
    )
    pol = ConfiguredRiskPolicy(s, portfolio_exposure=agg)
    ok, rc = pol.evaluate(_intent())
    assert ok is False
    assert rc == ReasonCode.RISK_PORTFOLIO_EXPOSURE_UNRESOLVED


def test_complete_but_e_portfolio_none_denies() -> None:
    s = _base_risk(max_portfolio_notional_usd_open=100.0)
    agg = _make_agg(
        PortfolioExposureAggregate(
            complete=True,
            pending_notional_usd=0.0,
            pending_complete=True,
            filled_net_exposure_usd=0.0,
            filled_complete=True,
            e_portfolio=None,
            error=None,
            omitted_instruments_unresolved_mark=(),
        ),
    )
    pol = ConfiguredRiskPolicy(s, portfolio_exposure=agg)
    ok, rc = pol.evaluate(_intent())
    assert ok is False
    assert rc == ReasonCode.RISK_PORTFOLIO_EXPOSURE_UNRESOLVED


def test_unsafe_complete_with_omissions_cap_check_proceeds(caplog: pytest.LogCaptureFixture) -> None:
    """Partial-marks underestimation: complete + omissions + unsafe → still enforce C vs E+n."""
    s = _base_risk(
        max_portfolio_notional_usd_open=100.0,
        fail_on_unresolved_portfolio_exposure=False,
    )
    agg = _make_agg(
        PortfolioExposureAggregate(
            complete=True,
            pending_notional_usd=0.0,
            pending_complete=True,
            filled_net_exposure_usd=0.0,
            filled_complete=True,
            e_portfolio=80.0,
            error=None,
            omitted_instruments_unresolved_mark=("0xabc-99.POLYMARKET",),
        ),
    )
    pol = ConfiguredRiskPolicy(s, portfolio_exposure=agg)
    with caplog.at_level("WARNING"):
        ok, rc = pol.evaluate(_intent(qty=10.0, price=2.0))
    assert ok is True
    assert rc == "approved"
    assert "underestimate" in caplog.text.lower() or "omitted" in caplog.text.lower()

    pol2 = ConfiguredRiskPolicy(s, portfolio_exposure=agg)
    ok2, rc2 = pol2.evaluate(_intent(qty=30.0, price=2.0))
    assert ok2 is False
    assert rc2 == ReasonCode.RISK_PORTFOLIO_NOTIONAL_CAP_EXCEEDED


def test_strict_complete_with_omissions_cap_unchanged() -> None:
    """Strict mode: if B1 still returned complete+ep (contract edge), cap applies; no extra deny."""
    s = _base_risk(
        max_portfolio_notional_usd_open=100.0,
        fail_on_unresolved_portfolio_exposure=True,
    )
    agg = _make_agg(
        PortfolioExposureAggregate(
            complete=True,
            pending_notional_usd=0.0,
            pending_complete=True,
            filled_net_exposure_usd=0.0,
            filled_complete=True,
            e_portfolio=85.0,
            error=None,
            omitted_instruments_unresolved_mark=("hypothetical.BOOK",),
        ),
    )
    pol = ConfiguredRiskPolicy(s, portfolio_exposure=agg)
    ok, rc = pol.evaluate(_intent(qty=10.0, price=1.0))
    assert ok is True
    assert rc == "approved"


def test_intent_notional_single_use_matches_evaluate_n() -> None:
    s = _base_risk(max_portfolio_notional_usd_open=100.0)
    seen: list[float | None] = []

    def agg_side(intent: OrderIntent, *, fail_on_unresolved: bool) -> PortfolioExposureAggregate:
        seen.append(float(intent.price_ref) * float(intent.quantity) if intent.price_ref else None)
        return PortfolioExposureAggregate(
            complete=True,
            pending_notional_usd=90.0,
            pending_complete=True,
            filled_net_exposure_usd=0.0,
            filled_complete=True,
            e_portfolio=90.0,
            error=None,
            omitted_instruments_unresolved_mark=(),
        )

    m = MagicMock()
    m.aggregate.side_effect = agg_side
    pol = ConfiguredRiskPolicy(s, portfolio_exposure=m)
    pol.evaluate(_intent(qty=2.0, price=0.5))
    assert seen == [1.0]


def test_cap_uses_e_portfolio_plus_n_not_double_pending() -> None:
    """B1 ``e_portfolio`` already includes pending; evaluate adds ``n`` once (§4.5)."""
    s = _base_risk(max_portfolio_notional_usd_open=100.0)
    agg = _make_agg(
        PortfolioExposureAggregate(
            complete=True,
            pending_notional_usd=60.0,
            pending_complete=True,
            filled_net_exposure_usd=0.0,
            filled_complete=True,
            e_portfolio=60.0,
            error=None,
            omitted_instruments_unresolved_mark=(),
        ),
    )
    pol = ConfiguredRiskPolicy(s, portfolio_exposure=agg)
    ok, rc = pol.evaluate(_intent(qty=10.0, price=5.0))
    assert ok is False
    assert rc == ReasonCode.RISK_PORTFOLIO_NOTIONAL_CAP_EXCEEDED


def test_missing_price_denies_when_portfolio_cap_needs_n() -> None:
    """Cap check uses ``n``; without ``price_ref``, deny ``RISK_MISSING_PRICE`` (not cap breach)."""
    s = replace(
        _base_risk(max_portfolio_notional_usd_open=50.0),
        fail_on_missing_price_for_notional=False,
    )
    agg = _make_agg(
        PortfolioExposureAggregate(
            complete=True,
            pending_notional_usd=0.0,
            pending_complete=True,
            filled_net_exposure_usd=0.0,
            filled_complete=True,
            e_portfolio=10.0,
            error=None,
            omitted_instruments_unresolved_mark=(),
        ),
    )
    pol = ConfiguredRiskPolicy(s, portfolio_exposure=agg)
    ok, rc = pol.evaluate(_intent(price=None))
    assert ok is False
    assert rc == ReasonCode.RISK_MISSING_PRICE


def test_no_aggregator_with_finite_cap_denies() -> None:
    s = _base_risk(max_portfolio_notional_usd_open=1.0)
    pol = ConfiguredRiskPolicy(s, portfolio_exposure=None)
    ok, rc = pol.evaluate(_intent())
    assert ok is False
    assert rc == ReasonCode.RISK_PORTFOLIO_EXPOSURE_UNRESOLVED


def test_b0_still_rejects_finite_portfolio_without_framework(tmp_path: Path) -> None:
    risk = _base_risk(max_portfolio_notional_usd_open=10.0)
    rt_path = tmp_path / "rt.yaml"
    rt_path.write_text(
        yaml.safe_dump(
            {
                "trader_id": "T-001",
                "execution_mode": "live",
                "polymarket_nautilus_live": True,
                "polymarket_framework_submit": False,
                "polymarket_instrument_ids": ["0xabc-1.POLYMARKET"],
            },
        ),
        encoding="utf-8",
    )
    runtime = load_runtime_settings(rt_path)
    with pytest.raises(ValueError, match="Phase B framework-truth gates require"):
        validate_phase_b_runtime_contract(risk, runtime)
