"""Z-Gap live config validation and enforce gate wiring (A0.1)."""

from __future__ import annotations

import os
from pathlib import Path

from tyrex_pm.runtime.config import AppConfig, ConfigError, Z_GAP_ENTRY_MODE_ENFORCE
from tyrex_pm.runtime.z_gap_preflight import enforce_blockers, load_z_gap_preflight_gates

DEFAULT_PREFLIGHT_DIR = Path("var/reporting/z_gap")


def z_gap_preflight_dir() -> Path:
    raw = os.environ.get("Z_GAP_PREFLIGHT_DIR", "").strip()
    if raw:
        return Path(raw)
    return DEFAULT_PREFLIGHT_DIR


def validate_z_gap_enforce_gates(
    *,
    artifacts_dir: Path | str | None = None,
) -> list[str]:
    """Return blockers for entry_mode: enforce (empty when all gates pass)."""
    base = artifacts_dir if artifacts_dir is not None else z_gap_preflight_dir()
    return enforce_blockers(artifacts_dir=base)


def validate_z_gap_live_config(app: AppConfig) -> None:
    """Fail closed when z_gap entry_mode: enforce without required preflight gates."""
    if app.z_gap is None:
        return
    if app.z_gap.entry_mode != Z_GAP_ENTRY_MODE_ENFORCE:
        return
    blockers = validate_z_gap_enforce_gates()
    if blockers:
        joined = "; ".join(blockers)
        raise ConfigError(
            f"z_gap entry_mode=enforce blocked by preflight gates: {joined}"
        )


def z_gap_enforce_gate_status(
    *,
    artifacts_dir: Path | str | None = None,
):
    """Expose gate evaluation for operators and tests."""
    base = artifacts_dir if artifacts_dir is not None else z_gap_preflight_dir()
    return load_z_gap_preflight_gates(base)
