"""Explicit, immutable provider qualification. No guessed terminal enums or units."""

from __future__ import annotations

from pathlib import Path

from ..capture.files import read_json, within
from ..contracts import digest


def load_policy(root: Path, collection: str) -> dict:
    path = within(root, f"collections/{collection}/provider_policy.json")
    if not path.exists():
        return {}
    policy = read_json(path)
    validate_policy(policy)
    return {**policy, "sha256": digest(path.read_bytes())}


def semantic_status(policy: dict, fingerprint: str | None) -> tuple[str, str | None]:
    """READY only when this file attests both the rule and the official threshold."""
    reason = policy.get("semantic_reason") or "official_threshold_and_payoff_contract_not_attested"
    fingerprints = policy.get("supported_rule_fingerprints") or []
    if (
        policy.get("rule_status") == "SUPPORTED"
        and policy.get("official_threshold") == "ATTESTED"
        and fingerprint
        and fingerprint in fingerprints
    ):
        return "READY", None
    return "UNSUPPORTED", reason


def validate_policy(policy: dict) -> None:
    required = {"schema_version", "policy_id", "evidence", "terminal_statuses", "payout_unit"}
    optional = {
        "rule_status",
        "official_threshold",
        "supported_rule_fingerprints",
        "semantic_reason",
        "payout_index_basis",
    }
    unknown = set(policy) - required - optional
    if not required <= set(policy) or unknown or policy["schema_version"] != 1:
        raise ValueError("unsupported provider policy fields/version")
    if not isinstance(policy["policy_id"], str) or not policy["policy_id"]:
        raise ValueError("policy identity required")
    if (
        not isinstance(policy["evidence"], list)
        or not policy["evidence"]
        or not all(isinstance(v, str) and v for v in policy["evidence"])
    ):
        raise ValueError("qualification evidence required")
    statuses = policy["terminal_statuses"]
    if (
        not isinstance(statuses, list)
        or not statuses
        or not all(isinstance(v, str) and v for v in statuses)
    ):
        raise ValueError("verified terminal statuses required")
    if policy["payout_unit"] != "USD_per_share":
        raise ValueError("unsupported payout unit")
    if "rule_status" in policy and policy["rule_status"] not in ("UNSUPPORTED", "SUPPORTED"):
        raise ValueError("rule_status must be UNSUPPORTED or SUPPORTED")
    threshold = policy.get("official_threshold")
    if "official_threshold" in policy and threshold not in ("UNAVAILABLE", "ATTESTED"):
        raise ValueError("official_threshold must be UNAVAILABLE or ATTESTED")
    if policy.get("rule_status") == "SUPPORTED" and policy.get("official_threshold") != "ATTESTED":
        raise ValueError("supported rules require an attested official threshold")
    if policy.get("official_threshold") == "ATTESTED" and policy.get("rule_status") != "SUPPORTED":
        raise ValueError("an attested threshold requires rule_status SUPPORTED")
    fingerprints = policy.get("supported_rule_fingerprints", [])
    if "supported_rule_fingerprints" in policy and (
        not isinstance(fingerprints, list)
        or not all(isinstance(value, str) and value for value in fingerprints)
    ):
        raise ValueError("rule fingerprints must be non-empty strings")
    if "payout_index_basis" in policy and policy["payout_index_basis"] != "outcome_index":
        raise ValueError("payout index basis must be outcome_index when declared")
    if "semantic_reason" in policy and not isinstance(policy["semantic_reason"], str):
        raise ValueError("semantic_reason must be a string")


def resolution_schedule(entity: dict, assessment: dict, now: int, settings) -> dict:
    """Persistent first-terminal plus 1h/24h audits; conflicts reopen polling."""
    result = {
        **entity,
        "resolution_status": assessment["status"],
        "resolution_assessment": assessment,
    }
    elapsed = (now - entity["window_end"]) // 10**9
    interval = (
        settings.poll_initial_s
        if elapsed < 600
        else settings.poll_after_10m_s
        if elapsed < 86400
        else settings.poll_after_24h_s
    )
    if assessment["status"] == "PROVIDER_TERMINAL":
        first = entity.get("terminal_first_at", now)
        completed = set(entity.get("terminal_audits_completed", []))
        for seconds in (3600, 86400):
            if now >= first + seconds * 10**9:
                completed.add(seconds)
        remaining = [first + s * 10**9 for s in (3600, 86400) if s not in completed]
        result.update(
            terminal_first_at=first,
            terminal_audits_completed=sorted(completed),
            resolution_next_at=min(remaining) if remaining else None,
        )
    else:
        result["resolution_next_at"] = now + interval * 10**9
    return result
