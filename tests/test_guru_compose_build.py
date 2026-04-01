"""Compose guru node without running the event loop."""

from __future__ import annotations

from pathlib import Path

import yaml

from tyrex_pm.config.loaders import (
    load_risk_settings,
    load_runtime_settings,
    load_strategy_settings,
)
from tyrex_pm.runtime.guru_compose import build_guru_trading_node


def test_compose_shadow_builds(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    strat = load_strategy_settings(root / "config" / "strategy" / "guru_follow.yaml")
    risk = load_risk_settings(root / "config" / "risk" / "guru_follow_risk.yaml")
    live = tmp_path / "live.yaml"
    live.write_text(
        yaml.safe_dump(
            {
                "trader_id": "TEST-CMP-001",
                "execution_mode": "shadow",
                "guru_poll_interval_seconds": 60.0,
            }
        ),
        encoding="utf-8",
    )
    runtime = load_runtime_settings(live)

    node, risk_pol = build_guru_trading_node(strat, risk, runtime)
    assert risk_pol is not None
    node.build()
    assert node.is_built
