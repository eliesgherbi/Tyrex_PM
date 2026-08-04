"""Run / state / recording / ops path helpers.

When ``var_root`` is provided (tests), it is the directory that directly contains
``runs/``, ``runtime_state/``, ``recordings/``, and ``logs/``.
When only ``repo_root`` is provided, paths are under ``<repo_root>/var/``.
"""

from __future__ import annotations

import re
from pathlib import Path

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def sanitize_segment(name: str, *, field: str = "name") -> str:
    cleaned = str(name).strip()
    if not cleaned or not _SAFE_NAME.match(cleaned):
        raise ValueError(f"invalid {field}: {name!r}")
    return cleaned


def _base(*, var_root: Path | None, repo_root: Path | None) -> Path:
    if var_root is not None:
        return Path(var_root)
    base = Path(repo_root) if repo_root is not None else Path.cwd()
    return base / "var"


def strategy_run_dir(
    *,
    strategy_id: str,
    run_id: str,
    var_root: Path | None = None,
    repo_root: Path | None = None,
) -> Path:
    root = _base(var_root=var_root, repo_root=repo_root)
    return (
        root
        / "runs"
        / sanitize_segment(strategy_id, field="strategy_id")
        / sanitize_segment(run_id, field="run_id")
    )


def ops_run_dir(
    *,
    check_or_tool_id: str,
    run_id: str,
    var_root: Path | None = None,
    repo_root: Path | None = None,
) -> Path:
    root = _base(var_root=var_root, repo_root=repo_root)
    return (
        root
        / "runs"
        / "_ops"
        / sanitize_segment(check_or_tool_id, field="check_or_tool_id")
        / sanitize_segment(run_id, field="run_id")
    )


def runtime_state_dir(
    *,
    kind: str,
    var_root: Path | None = None,
    repo_root: Path | None = None,
) -> Path:
    if kind not in {"shadow", "live"}:
        raise ValueError("kind must be 'shadow' or 'live'")
    return _base(var_root=var_root, repo_root=repo_root) / "runtime_state" / kind


def recordings_dir(
    *,
    recording_id: str,
    var_root: Path | None = None,
    repo_root: Path | None = None,
) -> Path:
    root = _base(var_root=var_root, repo_root=repo_root)
    return root / "recordings" / sanitize_segment(recording_id, field="recording_id")


def logs_dir(*, var_root: Path | None = None, repo_root: Path | None = None) -> Path:
    return _base(var_root=var_root, repo_root=repo_root) / "logs"


def ensure_run_layout(run_dir: Path, *, debug: bool = False) -> dict[str, Path]:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {
        "run_dir": run_dir,
        "manifest": run_dir / "manifest.json",
        "run_summary": run_dir / "run_summary.json",
        "audit_events": run_dir / "audit_events.jsonl",
        "analytics_events": run_dir / "analytics_events.jsonl",
    }
    paths["audit_events"].touch(exist_ok=True)
    paths["analytics_events"].touch(exist_ok=True)
    if debug:
        diag = run_dir / "diagnostics"
        diag.mkdir(parents=True, exist_ok=True)
        debug_path = diag / "debug_events.jsonl"
        debug_path.touch(exist_ok=True)
        paths["debug_events"] = debug_path
    return paths
