"""Exploratory trace helpers (M2B.4-B). Separate from strict decision outputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OVERFIT_WARNING = (
    "single-day 27-market sample; assumed latency and/or proxy formulas where applicable"
)

SAFETY_TAGS: dict[str, Any] = {
    "exploratory_only": True,
    "do_not_use_in_live_yaml": True,
    "overfit_warning": OVERFIT_WARNING,
}


def not_computable(metric: str, reason: str) -> dict[str, Any]:
    return {"metric": metric, "status": "not_computable", "reason": reason}


def wrap_exploratory(metrics: dict[str, Any]) -> dict[str, Any]:
    """Attach mandatory safety tags to an exploratory trace payload."""
    return {**SAFETY_TAGS, **metrics}


def exploratory_trace_md(trace: dict[str, Any]) -> str:
    lines = [
        "# EXPLORATORY TRACE",
        "",
        "> Exploratory only. Useful for intuition and M2B.5 hypothesis selection.",
        "> Not valid for live YAML or enforcement.",
        "",
        f"- exploratory_only: {trace.get('exploratory_only')}",
        f"- do_not_use_in_live_yaml: {trace.get('do_not_use_in_live_yaml')}",
        f"- overfit_warning: {trace.get('overfit_warning')}",
        "",
    ]
    skip = {"exploratory_only", "do_not_use_in_live_yaml", "overfit_warning"}
    for key, value in trace.items():
        if key in skip:
            continue
        if isinstance(value, dict) and value.get("status") == "not_computable":
            lines.append(f"- {key}: not_computable ({value.get('reason')})")
        else:
            lines.append(f"- {key}: {value}")
    return "\n".join(lines) + "\n"


def write_exploratory_trace(name: str, trace: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    wrapped = wrap_exploratory(trace)
    json_path = out_dir / f"{name}_exploratory_trace.json"
    md_path = out_dir / f"{name}_exploratory_trace.md"
    json_path.write_text(json.dumps(wrapped, indent=2, default=str), encoding="utf-8")
    md_path.write_text(exploratory_trace_md(wrapped), encoding="utf-8")
    return json_path, md_path
