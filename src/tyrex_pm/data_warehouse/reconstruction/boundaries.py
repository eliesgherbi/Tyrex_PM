"""Candidates preserve availability; no candidate is promoted to official attestation."""

from __future__ import annotations

from typing import Any

from ..contracts import exact, identity


def boundary_candidates(
    market: str, metadata: list[dict], references: list[dict], computed_at: int
) -> list[dict[str, Any]]:
    results = []
    versions = {}
    for row in metadata:
        if row.get("rule_fingerprint"):
            versions.setdefault(row["rule_fingerprint"], row)
    for rule, version in versions.items():
        for role, field in (("opening", "window_start"), ("closing", "window_end")):
            target = version.get(field)
            if target is None:
                continue
            nearby = [
                r
                for r in references
                if r.get("source_time") is not None
                and abs(r["source_time"] - target) <= 5_000_000_000
            ]
            if not nearby:
                results.append(
                    {
                        "boundary_observation_id": identity(market, rule, role, "missing"),
                        "market_id": market,
                        "boundary_role": role,
                        "target_boundary_at": target,
                        "value": None,
                        "unit": "USD",
                        "input_record_ids": [version["record_id"]],
                        "method": "unavailable",
                        "derivation_version": "1",
                        "rule_version_id": rule,
                        "available_at": version["received_at"],
                        "computed_at": computed_at,
                        "quality_status": "MISSING",
                        "officially_attested": False,
                    }
                )
            for reference in nearby:
                results.append(
                    {
                        "boundary_observation_id": identity(
                            market, rule, role, reference["record_id"]
                        ),
                        "market_id": market,
                        "boundary_role": role,
                        "target_boundary_at": target,
                        "value": reference["value"],
                        "unit": reference["unit"],
                        "input_record_ids": [version["record_id"], reference["record_id"]],
                        "method": "reference_candidate",
                        "derivation_version": "1",
                        "rule_version_id": rule,
                        "available_at": max(version["received_at"], reference["received_at"]),
                        "computed_at": computed_at,
                        "quality_status": "UNSUPPORTED_RULE",
                        "boundary_time_difference_ns": reference["source_time"] - target,
                        "officially_attested": False,
                    }
                )
    return results


def resolution_assessment(
    rows: list[dict], *, supported_statuses: tuple[str, ...] = (), payout_unit: str | None = None
) -> dict:
    if not rows:
        return {
            "status": "PENDING",
            "input_record_ids": [],
            "payouts": None,
            "chain_finality": "UNVERIFIED",
        }
    rows = sorted(rows, key=lambda row: (row["received_at"], row["record_id"]))
    vectors = {
        tuple(exact(p) for p in row["payouts"])
        for row in rows
        if row.get("payouts") and row.get("provider_status") in supported_statuses
    }
    latest = rows[-1]
    status = "REPORTED"
    if len(vectors) > 1:
        status = "CONFLICTING"
    elif latest.get("provider_status") in supported_statuses:
        vector = [exact(p) for p in latest.get("payouts", [])]
        if (
            payout_unit == "USD_per_share"
            and vector
            and len(vector) == 2
            and all(0 <= p <= 1 for p in vector)
            and sum(vector) == 1
        ):
            status = "PROVIDER_TERMINAL"
        else:
            status = "UNSUPPORTED"
    elif latest.get("payouts"):
        status = "UNSUPPORTED"
    return {
        "status": status,
        "input_record_ids": [r["record_id"] for r in rows],
        "payouts": latest.get("payouts") if status == "PROVIDER_TERMINAL" else None,
        "available_at": latest["received_at"],
        "chain_finality": "UNVERIFIED",
        "reason": None
        if status == "PROVIDER_TERMINAL"
        else "status_or_payout_contract_unqualified",
    }
