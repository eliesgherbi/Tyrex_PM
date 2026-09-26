"""Operator wording for startup recovery. Execution code never calls input()."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from tyrex_pm.execution.startup_reconciliation import RecoveryProposal, StartupAssessment


@dataclass(frozen=True)
class RecoveryDecision:
    approved: bool
    issue_id: str | None
    reason: str


def describe_assessment(assessment: StartupAssessment) -> str:
    lines = [assessment.summary]
    if assessment.wallet_display:
        lines.append(f"Account {assessment.wallet_display}")
    for issue in assessment.issues:
        if issue.severity.value == "AUTO":
            continue
        lines.append(f"- {issue.code}: {issue.summary}")
    for proposal in assessment.proposals:
        lines.append(
            f"Proposal {proposal.issue_id}: {proposal.action} up to "
            f"{proposal.max_shares} shares, price floor {proposal.min_price}."
        )
    return "\n".join(lines)


async def request_decision(
    assessment: StartupAssessment,
    *,
    live: bool,
    reader: Callable[[str], Awaitable[str | None]] | None,
    timeout_s: float,
) -> RecoveryDecision:
    """Default is decline. A missing terminal, EOF, or timeout does not approve."""
    if not assessment.proposals:
        return RecoveryDecision(False, None, "no_proposal")
    proposal = assessment.proposals[0]
    if not live:
        return RecoveryDecision(False, proposal.issue_id, "live_not_requested")
    if reader is None:
        return RecoveryDecision(False, proposal.issue_id, "no_terminal")
    prompt = (
        f"{describe_assessment(assessment)}\n"
        f"Approve {proposal.issue_id}? Type approve to continue: "
    )
    try:
        answer = await asyncio.wait_for(reader(prompt), timeout=timeout_s)
    except (asyncio.TimeoutError, EOFError):
        return RecoveryDecision(False, proposal.issue_id, "declined")
    if answer is None:
        return RecoveryDecision(False, proposal.issue_id, "declined")
    if answer.strip().lower() == "approve":
        return RecoveryDecision(True, proposal.issue_id, "approved")
    return RecoveryDecision(False, proposal.issue_id, "declined")


async def request_legacy_association(
    *,
    live: bool,
    reader: Callable[[str], Awaitable[str | None]] | None,
    timeout_s: float,
    wallet_display: str,
) -> bool:
    """One-time provenance prompt. Approval does not clear positions or trade."""
    if not live or reader is None:
        return False
    shown = wallet_display or "the authenticated wallet"
    prompt = (
        "This journal has no wallet binding. "
        f"Type associate to record {shown} as provenance only. "
        "This does not close positions or place orders: "
    )
    try:
        answer = await asyncio.wait_for(reader(prompt), timeout=timeout_s)
    except (asyncio.TimeoutError, EOFError):
        return False
    if answer is None:
        return False
    return answer.strip().lower() == "associate"


def proposal_for(assessment: StartupAssessment, issue_id: str | None) -> RecoveryProposal | None:
    if issue_id is None:
        return None
    for proposal in assessment.proposals:
        if proposal.issue_id == issue_id:
            return proposal
    return None
