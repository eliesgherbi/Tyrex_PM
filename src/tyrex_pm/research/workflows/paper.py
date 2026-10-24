"""Read-only paper preflight and status. Nothing here launches or mutates accounts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tyrex_pm.runtime.run_config import RunConfigError, load_trading_run_config

from .artifacts import REPO
from .contracts import WorkflowError

S0_REPORTS = (
    {
        "role": "fill_and_commit",
        "name": "step2_s0_reach_hold_paper_20261020T124243Z",
        "limitation": "stopped on hold commit; no raw context for reconstruction",
    },
    {
        "role": "first_restart",
        "name": "step2_s0_reach_hold_paper_20261020T124549Z",
        "limitation": "shares were kept and the process stopped",
    },
    {
        "role": "replacement",
        "name": "step2_s0_reach_hold_paper_20261020T124727Z",
        "limitation": (
            "tracked the same ledger and rolled; no second intentional restart; "
            "reconstruction refused"
        ),
    },
)
FOLLOW_UP = {
    "code": "step2_follow_up_open",
    "reason": "paper launch waits for the deferred accounting and reconstruction follow-up",
}


def _freshness(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return {"path": str(path), "bytes": stat.st_size, "modified_ns": stat.st_mtime_ns}


def paper_preflight(request: dict, root: Path) -> dict[str, Any]:
    path = Path(request["profile"])
    if not path.is_file():
        raise WorkflowError("not_found", "profile file is missing", {"path": str(path)})
    blockers = [dict(FOLLOW_UP)]
    schema_valid = False
    kind = None
    try:
        config = load_trading_run_config(path)
    except RunConfigError as exc:
        blockers.append({"code": "profile_invalid", "reason": str(exc)})
    else:
        schema_valid = True
        kind = config.strategy_kind
        if config.venue != "paper":
            blockers.append({"code": "venue", "reason": "preflight expected venue paper"})
    evidence = []
    base = (
        REPO / "var" / "research" / "trading_integrity" / "step2" / "paper" / "s0_reach" / "reports"
    )
    for item in S0_REPORTS:
        found = _freshness(base / item["name"])
        evidence.append(
            {**item, "present": found is not None, "freshness": found, "accepted": False}
        )
    return {
        "profile": str(path),
        "strategy_kind": kind,
        "schema_valid": schema_valid,
        "launch_allowed": False,
        "mutates_account": False,
        "blockers": blockers,
        "s0_observations": evidence,
    }


def paper_status(request: dict, root: Path) -> dict[str, Any]:
    report_dir = Path(request["report_directory"])
    reports = []
    if report_dir.is_dir():
        for path in sorted(report_dir.rglob("run_summary.json"))[:50]:
            freshness = _freshness(path)
            reports.append({"name": path.parent.name, "freshness": freshness})
    return {
        "report_directory": str(report_dir),
        "reports": reports,
        "launch_allowed": False,
        "mutates_account": False,
        "blockers": [dict(FOLLOW_UP)],
    }
