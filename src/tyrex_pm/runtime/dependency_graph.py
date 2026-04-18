from __future__ import annotations

from pathlib import Path

from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.execution.oms import OMS
from tyrex_pm.runtime.config import AppConfig, load_app_config


def build_config(repo_root: Path, *, scenario: str | None = None) -> AppConfig:
    return load_app_config(
        repo_root=repo_root,
        scenario_file=f"config/scenarios/{scenario}.yaml" if scenario else None,
    )


def build_shadow_oms() -> OMS:
    return OMS(ShadowOMS())
