"""Pure projection of a startup assessment. Never authorizes an order."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from tyrex_pm.execution.startup_reconciliation import StartupAssessment


def startup_report_payload(
    assessment: StartupAssessment,
    *,
    run_name: str,
    live_requested: bool,
    mutations: int = 0,
    venue: str | None = None,
    real_orders_authorized: bool | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_name": run_name,
        "status": assessment.status.value,
        "summary": assessment.summary,
        "wallet_display": assessment.wallet_display,
        "live_requested": live_requested,
        "venue": venue,
        "real_orders_authorized": real_orders_authorized,
        "mutation_attempts": mutations,
        "issues": [
            {
                "issue_id": issue.issue_id,
                "severity": issue.severity.value,
                "code": issue.code,
                "summary": issue.summary,
                "session_id": issue.session_id,
            }
            for issue in assessment.issues
        ],
        "proposals": [
            {
                "issue_id": proposal.issue_id,
                "session_id": proposal.session_id,
                "action": proposal.action,
                "condition_id": proposal.condition_id,
                "token_id": proposal.token_id,
                "max_shares": str(proposal.max_shares),
                "min_price": str(proposal.min_price),
                "venue_order_ids": list(proposal.venue_order_ids),
            }
            for proposal in assessment.proposals
        ],
    }


def write_startup_report(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(payload), indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
