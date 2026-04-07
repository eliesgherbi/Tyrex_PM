"""Phase B milestone B0: compose/runtime contract validation (no live deployment-budget exercise here)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from tyrex_pm.config.loaders import (
    framework_phase_b_eligible,
    load_risk_settings,
    load_runtime_settings,
    load_strategy_settings,
    phase_b_framework_truth_gates_active,
    validate_phase_b_runtime_contract,
)
from tyrex_pm.runtime.guru_compose import build_guru_trading_node


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
    base = {
        "trader_id": "T-001",
        "execution_mode": "shadow",
    }
    base.update(rt)
    p = tmp_path / "rt.yaml"
    p.write_text(yaml.safe_dump(base), encoding="utf-8")
    return p


def test_load_risk_defaults_phase_b_fields(tmp_path: Path) -> None:
    p = _risk_yaml(tmp_path)
    r = load_risk_settings(p)
    assert r.max_portfolio_notional_usd_open == float("inf")
    assert r.fail_on_unresolved_portfolio_deployment is True
    assert r.max_concurrent_guru_resting_orders is None
    assert r.collateral_reserve_usd == 0.0
    assert not phase_b_framework_truth_gates_active(r)


def test_load_risk_rejects_reserve_without_capital_gate(tmp_path: Path) -> None:
    p = _risk_yaml(tmp_path, collateral_reserve_usd=100.0)
    with pytest.raises(ValueError, match="collateral_reserve_usd > 0 requires capital_gate_enabled"):
        load_risk_settings(p)


def test_load_risk_reserve_ok_with_capital_gate(tmp_path: Path) -> None:
    p = _risk_yaml(
        tmp_path,
        collateral_reserve_usd=50.0,
        capital_gate_enabled=True,
    )
    r = load_risk_settings(p)
    assert r.collateral_reserve_usd == 50.0
    assert r.capital_gate_enabled is True


def test_load_rejects_concurrency_zero(tmp_path: Path) -> None:
    p = _risk_yaml(tmp_path, max_concurrent_guru_resting_orders=0)
    with pytest.raises(ValueError, match="max_concurrent_guru_resting_orders"):
        load_risk_settings(p)


def test_load_rejects_nonpositive_portfolio_cap(tmp_path: Path) -> None:
    p = _risk_yaml(tmp_path, max_portfolio_notional_usd_open=0.0)
    with pytest.raises(ValueError, match="max_portfolio_notional_usd_open"):
        load_risk_settings(p)


def test_phase_b_gates_active_when_portfolio_finite(tmp_path: Path) -> None:
    p = _risk_yaml(tmp_path, max_portfolio_notional_usd_open=1_000_000.0)
    r = load_risk_settings(p)
    assert phase_b_framework_truth_gates_active(r)


def test_phase_b_gates_active_when_concurrency_set(tmp_path: Path) -> None:
    p = _risk_yaml(tmp_path, max_concurrent_guru_resting_orders=3)
    r = load_risk_settings(p)
    assert phase_b_framework_truth_gates_active(r)


def test_validate_rejects_shadow_plus_framework_gates(tmp_path: Path) -> None:
    r = load_risk_settings(_risk_yaml(tmp_path, max_portfolio_notional_usd_open=500.0))
    run = load_runtime_settings(_runtime_yaml(tmp_path, execution_mode="shadow"))
    with pytest.raises(ValueError, match="shadow"):
        validate_phase_b_runtime_contract(r, run)


def test_validate_rejects_shadow_plus_reserve(tmp_path: Path) -> None:
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


def test_load_rejects_obsolete_polymarket_framework_submit_key(tmp_path: Path) -> None:
    p = _runtime_yaml(
        tmp_path,
        execution_mode="live",
        polymarket_framework_submit=False,
        polymarket_instrument_ids=["0xabc-1.POLYMARKET"],
    )
    with pytest.raises(ValueError, match="obsolete"):
        load_runtime_settings(p)


def test_validate_accepts_live_framework_triple(tmp_path: Path) -> None:
    r = load_risk_settings(
        _risk_yaml(
            tmp_path,
            max_portfolio_notional_usd_open=500.0,
            max_concurrent_guru_resting_orders=2,
        ),
    )
    run = load_runtime_settings(
        _runtime_yaml(
            tmp_path,
            execution_mode="live",
            polymarket_instrument_ids=["0xabc-1.POLYMARKET"],
        ),
    )
    assert framework_phase_b_eligible(run)
    validate_phase_b_runtime_contract(r, run)


def test_compose_shadow_rejects_phase_b_gate(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parent.parent
    strat_path = root / "config" / "strategy" / "guru_follow.yaml"
    strat = load_strategy_settings(strat_path)
    base_risk = load_risk_settings(root / "config" / "risk" / "guru_follow_risk.yaml")
    risk = replace(base_risk, max_portfolio_notional_usd_open=999.0)
    run = load_runtime_settings(
        _runtime_yaml(tmp_path, execution_mode="shadow", trader_id="X-SHAD-001"),
    )
    with pytest.raises(ValueError, match="shadow"):
        build_guru_trading_node(strat, risk, run)
