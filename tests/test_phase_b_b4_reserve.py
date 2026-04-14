"""Phase B B4: collateral reserve / free-after-reserve (py-clob balance, capital gate path only)."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from tyrex_pm.config.loaders import (
    RiskSettings,
    load_risk_settings,
    load_runtime_settings,
    validate_phase_b_runtime_contract,
)
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent
from tyrex_pm.risk.configured import ConfiguredRiskPolicy
from tyrex_pm.runtime.capital import DefaultCapitalStateProvider
from tyrex_pm.runtime.state_readers import AccountSnapshot, AllowanceSnapshot


def _risk(**over: object) -> RiskSettings:
    base: dict[str, object] = {
        "max_notional_usd_per_order": 10_000.0,
        "max_token_notional_usd_open": float("inf"),
        "kill_switch": False,
        "fail_on_missing_price_for_notional": True,
        "capital_gate_enabled": True,
        "max_account_snapshot_age_seconds": 30.0,
        "max_allowance_snapshot_age_seconds": 120.0,
        "min_collateral_balance_usd": None,
        "min_allowance_usd": None,
        "fail_on_unresolved_token_deployment": False,
        "max_portfolio_notional_usd_open": float("inf"),
        "fail_on_unresolved_portfolio_deployment": True,
        "max_concurrent_guru_resting_orders": None,
        "collateral_reserve_usd": 0.0,
    }
    base.update(over)
    return RiskSettings(**base)  # type: ignore[arg-type]


def _acct_present() -> MagicMock:
    acct = MagicMock()
    acct.snapshot.return_value = AccountSnapshot(
        venue="POLYMARKET",
        captured_at_utc=datetime.now(tz=UTC),
        account_present=True,
        balances={},
        raw_summary=None,
    )
    return acct


def _allow_raw(balance: str, allowance: str = "999999") -> MagicMock:
    prov = MagicMock()
    prov.snapshot.return_value = AllowanceSnapshot(
        captured_at_utc=datetime.now(tz=UTC),
        raw={"balance": balance, "allowance": allowance},
    )
    return prov


def _capital(acct: MagicMock, allow: MagicMock | None) -> DefaultCapitalStateProvider:
    return DefaultCapitalStateProvider(acct, allow, observability_include_clob=True)


def _intent_buy(qty: float = 1.0, price: float | None = 0.5) -> OrderIntent:
    return OrderIntent(
        correlation_id="c",
        token_id="88888",
        side="BUY",
        quantity=qty,
        signal_kind="entry",
        reason_code="ok",
        price_ref=price,
    )


def _intent_sell(qty: float = 1.0, price: float = 0.5) -> OrderIntent:
    return OrderIntent(
        correlation_id="c",
        token_id="88888",
        side="SELL",
        quantity=qty,
        signal_kind="exit",
        reason_code="ok",
        price_ref=price,
    )


def test_reserve_zero_no_reserve_deny() -> None:
    """Reserve off: sufficient balance is not required for free-after-reserve (only mins if set)."""
    pol = ConfiguredRiskPolicy(
        _risk(collateral_reserve_usd=0.0),
        capital_provider=_capital(_acct_present(), _allow_raw("0.01")),
    )
    ok, rc, _ = pol.evaluate(_intent_buy(qty=10.0, price=0.5))  # n=5
    assert ok is True
    assert rc == "approved"


def test_reserve_allows_when_balance_above_reserve_plus_n() -> None:
    pol = ConfiguredRiskPolicy(
        _risk(collateral_reserve_usd=50.0),
        capital_provider=_capital(_acct_present(), _allow_raw("150.0")),
    )
    ok, rc, _ = pol.evaluate(_intent_buy(qty=10.0, price=5.0))  # n=50, need bal >= 100
    assert ok is True
    assert rc == "approved"


def test_reserve_allows_when_balance_exactly_reserve_plus_n() -> None:
    pol = ConfiguredRiskPolicy(
        _risk(collateral_reserve_usd=50.0),
        capital_provider=_capital(_acct_present(), _allow_raw("100.0")),
    )
    ok, rc, _ = pol.evaluate(_intent_buy(qty=10.0, price=5.0))  # n=50, reserve+n=100
    assert ok is True
    assert rc == "approved"


def test_reserve_denies_when_balance_below_reserve_plus_n() -> None:
    pol = ConfiguredRiskPolicy(
        _risk(collateral_reserve_usd=50.0),
        capital_provider=_capital(_acct_present(), _allow_raw("99.99")),
    )
    # n = 10 * 5.0 = 50; reserve + n = 100 > 99.99
    ok, rc, _ = pol.evaluate(_intent_buy(qty=10.0, price=5.0))
    assert ok is False
    assert rc == ReasonCode.RISK_INSUFFICIENT_FREE_COLLATERAL_AFTER_RESERVE


def test_reserve_deny_emits_ops_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="tyrex_pm.risk.configured")
    pol = ConfiguredRiskPolicy(
        _risk(collateral_reserve_usd=50.0),
        capital_provider=_capital(_acct_present(), _allow_raw("99.99")),
    )
    it = _intent_buy(qty=10.0, price=5.0)
    ok, rc, _ = pol.evaluate(it)
    assert ok is False
    assert rc == ReasonCode.RISK_INSUFFICIENT_FREE_COLLATERAL_AFTER_RESERVE
    joined = " ".join(r.message for r in caplog.records)
    assert "tyrex_risk_ops" in joined
    assert "gate=reserve" in joined
    assert "free_collateral_usd=" in joined
    assert "reserve_usd=50" in joined
    assert "required_free=100" in joined
    assert it.correlation_id in joined


def test_reserve_fail_closed_no_allowance_provider() -> None:
    pol = ConfiguredRiskPolicy(
        _risk(collateral_reserve_usd=10.0, min_collateral_balance_usd=None, min_allowance_usd=None),
        capital_provider=_capital(_acct_present(), None),
    )
    ok, rc, _ = pol.evaluate(_intent_buy())
    assert ok is False
    assert rc == ReasonCode.RISK_ALLOWANCE_UNAVAILABLE


def test_reserve_fail_closed_snapshot_none() -> None:
    prov = MagicMock()
    prov.snapshot.return_value = None  # type: ignore[assignment]
    pol = ConfiguredRiskPolicy(
        _risk(collateral_reserve_usd=10.0),
        capital_provider=_capital(_acct_present(), prov),
    )
    ok, rc, _ = pol.evaluate(_intent_buy())
    assert ok is False
    assert rc == ReasonCode.RISK_ALLOWANCE_UNAVAILABLE


def test_reserve_fail_closed_unparsable_balance() -> None:
    pol = ConfiguredRiskPolicy(
        _risk(collateral_reserve_usd=10.0),
        capital_provider=_capital(_acct_present(), _allow_raw("not-a-float")),
    )
    ok, rc, _ = pol.evaluate(_intent_buy())
    assert ok is False
    assert rc == ReasonCode.RISK_ALLOWANCE_UNAVAILABLE


def test_reserve_missing_price_buy_same_as_portfolio_cap_contract() -> None:
    """BUY with reserve needs n; align with B2 (RISK_MISSING_PRICE), not a permissive reserve skip."""
    pol = ConfiguredRiskPolicy(
        _risk(
            collateral_reserve_usd=10.0,
            fail_on_missing_price_for_notional=False,
        ),
        capital_provider=_capital(_acct_present(), _allow_raw("1000.0")),
    )
    intent = replace(_intent_buy(), price_ref=None)
    ok, rc, _ = pol.evaluate(intent)
    assert ok is False
    assert rc == ReasonCode.RISK_MISSING_PRICE


def test_reserve_sell_not_subject_to_free_after_reserve_math() -> None:
    pol = ConfiguredRiskPolicy(
        _risk(collateral_reserve_usd=10_000.0),
        capital_provider=_capital(_acct_present(), _allow_raw("5.0")),
    )
    ok, rc, _ = pol.evaluate(_intent_sell(qty=1.0, price=0.5))
    assert ok is True
    assert rc == "approved"


def test_misconfigured_reserve_without_capital_gate_fail_closed() -> None:
    """Defensive: loader forbids this; direct settings still must not bypass silently."""
    s = _risk(capital_gate_enabled=False, collateral_reserve_usd=25.0)
    pol = ConfiguredRiskPolicy(
        s,
        capital_provider=_capital(_acct_present(), _allow_raw("999.0")),
    )
    ok, rc, _ = pol.evaluate(_intent_buy())
    assert ok is False
    assert rc == ReasonCode.RISK_ALLOWANCE_UNAVAILABLE


def _risk_yaml(tmp_path: Path, **risk: object) -> Path:
    base = {
        "max_notional_usd_per_order": 5.0,
        "max_token_notional_usd_open": 20.0,
    }
    base.update(risk)
    p = tmp_path / "risk.yaml"
    p.write_text(yaml.safe_dump(base), encoding="utf-8")
    return p


def _runtime_yaml(tmp_path: Path, **rt: object) -> Path:
    base = {"trader_id": "T-001", "execution_mode": "shadow"}
    base.update(rt)
    p = tmp_path / "rt.yaml"
    p.write_text(yaml.safe_dump(base), encoding="utf-8")
    return p


def test_b0_reserve_requires_capital_gate(tmp_path: Path) -> None:
    p = _risk_yaml(tmp_path, collateral_reserve_usd=100.0)
    with pytest.raises(ValueError, match="collateral_reserve_usd > 0 requires capital_gate_enabled"):
        load_risk_settings(p)


def test_b0_shadow_plus_reserve_invalid(tmp_path: Path) -> None:
    r = load_risk_settings(
        _risk_yaml(
            tmp_path,
            collateral_reserve_usd=10.0,
            capital_gate_enabled=True,
        ),
    )
    run = load_runtime_settings(_runtime_yaml(tmp_path, execution_mode="shadow"))
    with pytest.raises(ValueError, match="collateral_reserve_usd > 0"):
        validate_phase_b_runtime_contract(r, run)
