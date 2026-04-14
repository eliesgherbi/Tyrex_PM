"""WP1 — ``run_guru`` finally must still ``node.stop`` if drain raises."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def _load_run_guru(repo_root: Path):
    p = repo_root / "scripts" / "run_guru.py"
    spec = importlib.util.spec_from_file_location("run_guru_wp1_test", p)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_run_guru_finally_calls_node_stop_when_drain_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    repo = Path(__file__).resolve().parents[2]
    rg = _load_run_guru(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_guru",
            "--strategy-conf",
            "config/scenarios/stabilization_wave1/guru_follow.yaml",
            "--risk-conf",
            "config/scenarios/stabilization_wave1/guru_follow_risk.yaml",
            "--live-conf",
            "config/scenarios/stabilization_wave1/live_polymarket.yaml",
        ],
    )

    node = MagicMock()
    node.run.side_effect = KeyboardInterrupt()
    assembly = MagicMock()
    assembly.node = node
    assembly.execution_lifecycle.nonzero_exit_requested = False

    fake_coord = MagicMock()
    fake_coord.stop = MagicMock()

    def fake_coord_cls(**kwargs: object) -> MagicMock:
        return fake_coord

    with (
        patch("tyrex_pm.runtime.guru_compose.build_guru_trading_node", return_value=assembly),
        patch(
            "tyrex_pm.runtime.guru_shutdown.drain_before_node_stop",
            side_effect=RuntimeError("drain boom"),
        ),
        patch("tyrex_pm.runtime.lifecycle.StartupReadinessCoordinator", fake_coord_cls),
    ):
        _ = rg.main()

    node.stop.assert_called_once()
    fake_coord.stop.assert_called_once()
