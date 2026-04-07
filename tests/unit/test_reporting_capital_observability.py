"""Capital observability helpers and risk → reporting integration."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from tyrex_pm.config.loaders import RiskSettings
from tyrex_pm.core.types import OrderIntent
from tyrex_pm.reporting.capital_observability import (
    compute_buy_headroom_usd,
    parse_risk_capital_flags_from_config_json,
    trim_json_text,
    venue_denial_insufficient_balance_likely,
)
from tyrex_pm.risk.configured import ConfiguredRiskPolicy
from tyrex_pm.runtime.state_readers import AccountSnapshot, AllowanceSnapshot


def test_trim_json_text_truncates() -> None:
    big = {"x": "y" * 5000}
    s = trim_json_text(big, max_chars=100)
    assert s is not None
    assert len(s) == 100
    assert s.endswith("...")


def test_venue_denial_heuristic() -> None:
    assert venue_denial_insufficient_balance_likely("Insufficient balance for tradenonce") is True
    assert venue_denial_insufficient_balance_likely("stale order") is False


def test_parse_config_capital_flags() -> None:
    cfg = json.dumps({"risk": {"capital_gate_enabled": False}, "runtime": {}})
    got = parse_risk_capital_flags_from_config_json(cfg)
    assert got["parse_ok"] is True
    assert got["capital_gate_enabled"] is False


def test_compute_buy_headroom() -> None:
    assert compute_buy_headroom_usd(100.0, 10.0, 25.0) == 65.0
    assert compute_buy_headroom_usd(None, 0.0, 1.0) is None


def test_risk_eval_emits_account_snapshot_when_observability_on_gate_off() -> None:
    """Gate off + observability: still records wallet snapshots for reporting (best-effort)."""
    rows: list[tuple[str, dict]] = []

    def fe(ft: str, pl: dict) -> None:
        rows.append((ft, pl))

    acct = AccountSnapshot(
        venue="POLYMARKET",
        captured_at_utc=datetime.now(tz=UTC),
        account_present=True,
        balances={"k": 1},
        raw_summary=None,
    )

    class AS:
        def snapshot(self) -> AccountSnapshot:
            return acct

    allow = AllowanceSnapshot(
        captured_at_utc=datetime.now(tz=UTC),
        raw={"balance": "42.5", "allowance": "100"},
    )

    class AP:
        def snapshot(self) -> AllowanceSnapshot:
            return allow

    rs = RiskSettings(
        max_notional_usd_per_order=1000.0,
        max_token_notional_usd_open=float("inf"),
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
        capital_gate_enabled=False,
    )
    intent = OrderIntent(
        correlation_id="c-test",
        token_id="t1",
        side="BUY",
        quantity=2.0,
        signal_kind="entry",
        reason_code="ok",
        price_ref=0.5,
    )
    pol = ConfiguredRiskPolicy(
        rs,
        account_snapshot=AS(),
        allowance_provider=AP(),
        fact_emit=fe,
        reporting_capital_observability_enabled=True,
        reporting_capital_snapshot_period_seconds=0.0,
        allowance_observability_enabled=True,
    )
    ok, _rc, _ = pol.evaluate(intent)
    assert ok is True
    types = [t for t, _ in rows]
    assert types.count("account_snapshot") >= 1
    assert types.count("risk_decision") == 1
    snap = next(p for t, p in rows if t == "account_snapshot")
    assert snap["snapshot_trigger"] == "risk_eval"
    assert snap["py_clob_balance_usd"] == 42.5
    rsk = next(p for t, p in rows if t == "risk_decision")
    assert rsk["capital_gate_enabled"] is False
    assert rsk["account_snapshot_seq"] == snap["account_snapshot_seq"]
    assert rsk["py_clob_balance_usd"] == 42.5


def test_capital_facts_prefer_nautilus_free_over_clob_atomic() -> None:
    """CLOB atomic string normalized; canonical follows Nautilus USDC.e free when present."""
    rows: list[tuple[str, dict]] = []

    def fe(ft: str, pl: dict) -> None:
        rows.append((ft, pl))

    acct = AccountSnapshot(
        venue="POLYMARKET",
        captured_at_utc=datetime.now(tz=UTC),
        account_present=True,
        balances={
            "events": [
                {
                    "balances": [
                        {"currency": "USDC.e", "free": "0.423789", "locked": "0"},
                    ],
                },
            ],
        },
        raw_summary=None,
    )

    class AS:
        def snapshot(_self) -> AccountSnapshot:
            return acct

    allow = AllowanceSnapshot(
        captured_at_utc=datetime.now(tz=UTC),
        raw={"balance": "423789", "allowance": None},
    )

    class AP:
        def snapshot(_self) -> AllowanceSnapshot:
            return allow

    rs = RiskSettings(
        max_notional_usd_per_order=1000.0,
        max_token_notional_usd_open=float("inf"),
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
        capital_gate_enabled=False,
    )
    intent = OrderIntent(
        correlation_id="c-at",
        token_id="t1",
        side="BUY",
        quantity=1.0,
        signal_kind="entry",
        reason_code="ok",
        price_ref=0.5,
    )
    pol = ConfiguredRiskPolicy(
        rs,
        account_snapshot=AS(),
        allowance_provider=AP(),
        fact_emit=fe,
        reporting_capital_observability_enabled=True,
        reporting_capital_snapshot_period_seconds=0.0,
        allowance_observability_enabled=True,
    )
    pol.evaluate(intent)
    rsk = next(p for t, p in rows if t == "risk_decision")
    assert abs(float(rsk["py_clob_balance_usd"]) - 0.423789) < 1e-9
    assert abs(float(rsk["nautilus_cash_free_usd"]) - 0.423789) < 1e-9
    assert abs(float(rsk["balance_canonical_usd"]) - 0.423789) < 1e-9
    assert rsk["capital_canonical_balance_source"] == "nautilus_cash_account"
