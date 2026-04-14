"""Phase 1 — :class:`~tyrex_pm.runtime.capital.DefaultCapitalStateProvider`."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from tyrex_pm.config.loaders import RiskSettings
from tyrex_pm.runtime.capital import CapitalStateSource, DefaultCapitalStateProvider
from tyrex_pm.runtime.capital.policy import CapitalSnapshotPolicy
from tyrex_pm.runtime.capital.provider import _need_clob_for_risk_gate
from tyrex_pm.runtime.state_readers import AccountSnapshot, AllowanceSnapshot


def _risk(**over: object) -> RiskSettings:
    base: dict[str, object] = {
        "max_notional_usd_per_order": 1000.0,
        "max_token_notional_usd_open": float("inf"),
        "kill_switch": False,
        "fail_on_missing_price_for_notional": True,
        "capital_gate_enabled": False,
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


def test_need_clob_for_risk_gate() -> None:
    r0 = _risk()
    p0 = CapitalSnapshotPolicy.from_risk_settings(r0)
    assert _need_clob_for_risk_gate(p0) is False
    assert _need_clob_for_risk_gate(CapitalSnapshotPolicy.from_risk_settings(_risk(min_collateral_balance_usd=1.0))) is True
    assert _need_clob_for_risk_gate(CapitalSnapshotPolicy.from_risk_settings(_risk(min_allowance_usd=1.0))) is True
    assert _need_clob_for_risk_gate(CapitalSnapshotPolicy.from_risk_settings(_risk(collateral_reserve_usd=1.0))) is True


def test_freshness_ok_adapter_only() -> None:
    acct = MagicMock()
    acct.snapshot.return_value = AccountSnapshot(
        venue="POLYMARKET",
        captured_at_utc=datetime.now(tz=UTC),
        account_present=True,
        balances={"c": {"currency": "USDC", "free": "10.0"}},
        raw_summary=None,
    )
    r = _risk()
    pol = CapitalSnapshotPolicy.from_risk_settings(r)
    prov = DefaultCapitalStateProvider(acct, None, observability_include_clob=False)
    cap = prov.snapshot(purpose="risk_gate", policy=pol)
    assert cap.ok is True
    assert cap.source == CapitalStateSource.ADAPTER_ACCOUNT
    assert cap.free_collateral_usd == 10.0
    assert cap.allowance_usd is None
    assert prov.freshness_ok(cap, policy=pol) is True
    stale_cap = replace(cap, captured_at_utc=cap.captured_at_utc - timedelta(seconds=60))
    assert prov.freshness_ok(stale_cap, policy=pol) is False


def test_risk_gate_account_missing_not_ok() -> None:
    acct = MagicMock()
    acct.snapshot.return_value = AccountSnapshot(
        venue="POLYMARKET",
        captured_at_utc=datetime.now(tz=UTC),
        account_present=False,
        balances=None,
        raw_summary=None,
    )
    r = _risk()
    pol = CapitalSnapshotPolicy.from_risk_settings(r)
    prov = DefaultCapitalStateProvider(acct, None)
    cap = prov.snapshot(purpose="risk_gate", policy=pol)
    assert cap.ok is False
    assert cap.error == "account_unavailable"


def test_risk_gate_need_clob_no_provider() -> None:
    acct = MagicMock()
    acct.snapshot.return_value = AccountSnapshot(
        venue="POLYMARKET",
        captured_at_utc=datetime.now(tz=UTC),
        account_present=True,
        balances={},
        raw_summary=None,
    )
    r = _risk(min_allowance_usd=10.0)
    pol = CapitalSnapshotPolicy.from_risk_settings(r)
    prov = DefaultCapitalStateProvider(acct, None)
    cap = prov.snapshot(purpose="risk_gate", policy=pol)
    assert cap.ok is False
    assert cap.error == "allowance_source_unavailable"


def test_observability_merges_clob_when_enabled() -> None:
    acct = MagicMock()
    acct.snapshot.return_value = AccountSnapshot(
        venue="POLYMARKET",
        captured_at_utc=datetime.now(tz=UTC),
        account_present=True,
        balances={},
        raw_summary=None,
    )
    allow = MagicMock()
    allow.snapshot.return_value = AllowanceSnapshot(
        captured_at_utc=datetime.now(tz=UTC),
        raw={"balance": "42", "allowance": "100"},
    )
    r = _risk()
    pol = CapitalSnapshotPolicy.from_risk_settings(r)
    prov = DefaultCapitalStateProvider(acct, allow, observability_include_clob=True)
    cap = prov.snapshot(purpose="observability", policy=pol)
    assert cap.merged_clob is True
    assert cap.source == CapitalStateSource.EXPLICIT_REFRESH
    assert cap.py_clob_balance_usd == 42 / 1_000_000.0


def test_separate_ttl_clob_not_called_second_time() -> None:
    acct = MagicMock()
    acct.snapshot.return_value = AccountSnapshot(
        venue="POLYMARKET",
        captured_at_utc=datetime.now(tz=UTC),
        account_present=True,
        balances={},
        raw_summary=None,
    )
    allow = MagicMock()
    allow.snapshot.return_value = AllowanceSnapshot(
        captured_at_utc=datetime.now(tz=UTC) - timedelta(seconds=5),
        raw={"balance": "1000000", "allowance": "1000000"},
    )
    r = _risk(min_collateral_balance_usd=0.5, max_allowance_snapshot_age_seconds=60.0)
    pol = CapitalSnapshotPolicy.from_risk_settings(r)
    prov = DefaultCapitalStateProvider(acct, allow)
    prov.snapshot(purpose="risk_gate", policy=pol)
    prov.snapshot(purpose="risk_gate", policy=pol)
    assert allow.snapshot.call_count == 1
