from __future__ import annotations

from pathlib import Path

from decimal import Decimal

from tyrex_pm.runtime.config import load_app_config


def test_scenario_overlay_execution_mode() -> None:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root, scenario_file="config/scenarios/live_guru.yaml")
    from tyrex_pm.core.enums import ExecutionMode

    assert app.runtime.execution_mode == ExecutionMode.LIVE
    assert app.risk.deployment.token_cap_usd == Decimal("100")


def test_scenario_bare_name_resolves() -> None:
    root = Path(__file__).resolve().parents[1]
    from tyrex_pm.core.enums import ExecutionMode

    by_name = load_app_config(repo_root=root, scenario_file="live_guru")
    by_path = load_app_config(repo_root=root, scenario_file="config/scenarios/live_guru.yaml")
    assert by_name.runtime.execution_mode == ExecutionMode.LIVE
    assert by_name.risk.deployment.token_cap_usd == by_path.risk.deployment.token_cap_usd
