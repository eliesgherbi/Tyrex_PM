#!/usr/bin/env python3
"""Phase 2 WS-primary live preflight before paired-binary sanity rerun."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(SCRIPTS))

from tyrex_pm.runtime.config import load_app_config  # noqa: E402

# Reuse M8 market/config checks (same WS-primary posture).
from m8_preflight_check import _git_sha, _sha256, _verify_market_books  # noqa: E402


def _check_wallet(errors: list[str], *, min_usd: Decimal) -> None:
    """Best-effort CLOB collateral check when live credentials are configured."""
    try:
        from tyrex_pm.venue.polymarket.clob_client_factory import build_live_clob_client
        from tyrex_pm.venue.polymarket.clob_wallet_sync import _v2_balance_to_usd
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    except ImportError:
        print("  [SKIP] wallet check — live deps not installed")
        return

    try:
        client = build_live_clob_client()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        client.update_balance_allowance(params)
        payload = client.get_balance_allowance(params)
        balance, allowance = _v2_balance_to_usd(payload)
        eff = min(balance, allowance) if allowance is not None else balance
        print(f"  wallet effective USD (min balance, allowance): {eff:.4f}")
        if eff < min_usd:
            errors.append(f"wallet effective {eff:.4f} USD < required {min_usd} USD")
    except Exception as exc:
        errors.append(f"wallet check failed: {exc!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2 WS-primary live preflight")
    parser.add_argument(
        "--scenario",
        default="live_paired_binary_tiny_ws_primary",
        help="Scenario under config/scenarios/ (default: live_paired_binary_tiny_ws_primary)",
    )
    parser.add_argument(
        "--skip-market-books",
        action="store_true",
        help="Skip live CLOB book/notional verification",
    )
    parser.add_argument(
        "--skip-wallet",
        action="store_true",
        help="Skip CLOB wallet balance/allowance check",
    )
    args = parser.parse_args()

    strategy_path = REPO / "config" / "strategies" / "paired_binary.yaml"
    scenario_path = REPO / "config" / "scenarios" / f"{args.scenario}.yaml"
    if not scenario_path.is_file():
        print(f"Scenario not found: {scenario_path}")
        return 1

    app = load_app_config(
        repo_root=REPO,
        strategy_file=str(strategy_path.relative_to(REPO)),
        scenario_file=str(scenario_path.relative_to(REPO)),
    )
    pb = app.paired_binary
    md = app.runtime.market_data
    errors: list[str] = []

    checks = {
        "websocket.primary_enabled": md.websocket.primary_enabled is True,
        "websocket.shadow_enabled": md.websocket.shadow_enabled is False,
        "rest.poll_enabled": md.rest.poll_enabled is False,
        "rest.bootstrap_on_startup": md.rest.bootstrap_on_startup is True,
        "rest.recovery_on_reconnect": md.rest.recovery_on_reconnect is True,
        "quality.enforcement_mode": md.quality.enforcement_mode == "enforce",
        "quality.require_ws_primary_for_entry": md.quality.require_ws_primary_for_entry is True,
        "quality.allow_rest_recovery_for_entry": md.quality.allow_rest_recovery_for_entry is False,
        "quality.allow_rest_recovery_for_exit": md.quality.allow_rest_recovery_for_exit is True,
        "market_data.enabled": md.enabled is True,
        "features.v0_enabled": md.features.v0_enabled is True,
        "observability.emit_decision_snapshot": app.runtime.observability.emit_decision_snapshot is True,
        "planner.enabled": app.execution.planner.enabled is True,
        "planner.use_executable_depth": app.execution.planner.use_executable_depth is True,
        "paired_binary.max_decision_rate_per_market_ms": (
            app.runtime.paired_binary.max_decision_rate_per_market_ms == 75
        ),
    }
    for key, ok in checks.items():
        if not ok:
            errors.append(f"config check failed: {key}")

    if pb is None:
        errors.append("paired_binary config missing")
    elif pb.position_size > 10:
        errors.append(f"position_size {pb.position_size} exceeds safe tiny cap (10)")

    print("Phase 2 live preflight")
    print(f"  git_commit: {_git_sha()}")
    print(f"  strategy: {strategy_path} sha256={_sha256(strategy_path)[:16]}...")
    print(f"  scenario: {scenario_path} sha256={_sha256(scenario_path)[:16]}...")
    if pb:
        print(f"  market_id: {pb.market_id}")
        print(f"  yes_token_id: {pb.yes_token_id}")
        print(f"  no_token_id: {pb.no_token_id}")
        print(f"  position_size: {pb.position_size}")
        print(f"  max_runtime_s: {pb.max_runtime_s}")
        print(f"  pair_stop_loss_pct: {pb.pair_stop_loss_pct}")
        print(f"  pair_take_profit_pct: {pb.pair_take_profit_pct}")
        print(f"  min_notional_usd: {app.risk.notional.min_usd}")

    for key, ok in checks.items():
        print(f"  [{('OK' if ok else 'FAIL')}] {key}")

    if not args.skip_market_books:
        print("\nMarket book verification (CLOB):")
        _verify_market_books(app, errors)

    if not args.skip_wallet and pb is not None:
        print("\nWallet verification (CLOB):")
        pair_cost = pb.position_size * Decimal("1.05")
        _check_wallet(errors, min_usd=pair_cost)

    if errors:
        print("\nBLOCKED:")
        for e in errors:
            print(f"  - {e}")
        print(
            "\nIf market books failed, run: python scripts/m8_market_preflight.py "
            "and pin yes_token_id/no_token_id in the scenario strategy overlay."
        )
        return 1

    print("\nPreflight PASSED — safe to start Phase 2 WS-primary live rerun.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
