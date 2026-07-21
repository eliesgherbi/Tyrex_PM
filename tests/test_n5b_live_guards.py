"""N5B structural guards: ShadowOMS only, no LiveOMS, depth-walk model, CLI exists."""

from __future__ import annotations

import ast
from pathlib import Path

import tyrex_pm
from tyrex_pm.execution.shadow_fill_model import FILL_MODEL_DEPTH_WALK_V1
from tyrex_pm.runtime.config import load_observe_config

ROOT = Path(tyrex_pm.__file__).resolve().parents[2]


def test_n5b_live_cli_exists() -> None:
    assert (ROOT / "tools" / "n5_shadow" / "run_n5_shadow_live.py").is_file()


def test_n5b_config_is_shadow_depth_walk() -> None:
    cfg = load_observe_config(ROOT / "config" / "observe_shadow_z_gap_n5b_live.json")
    assert cfg.shadow is not None
    assert cfg.shadow.enable_oms is True
    assert cfg.shadow.fill_model_id == FILL_MODEL_DEPTH_WALK_V1
    assert cfg.risk is not None
    assert cfg.risk.runtime_mode.value == "SHADOW"


def test_n5_runtime_module_has_no_liveoms_import() -> None:
    text = (ROOT / "src" / "tyrex_pm" / "runtime" / "n5_shadow_runtime.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(text)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    assert not any("live_oms" in m for m in imports)
    assert not any(m.startswith("old") for m in imports)


def test_n5b_live_cli_help_mentions_shadow_only() -> None:
    text = (ROOT / "tools" / "n5_shadow" / "run_n5_shadow_live.py").read_text(
        encoding="utf-8"
    )
    assert "ShadowOMS" in text or "SHADOW" in text
    assert "orders_live" in text
    assert "LiveOMS" in text  # mentioned as forbidden
    assert FILL_MODEL_DEPTH_WALK_V1 in text
