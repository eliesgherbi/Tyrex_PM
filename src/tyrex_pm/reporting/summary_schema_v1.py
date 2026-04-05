"""summary.json v1 shape (SCH-04)."""

from __future__ import annotations

from typing import Any

SUMMARY_SCHEMA_VERSION = 1

_SUMMARY_TOP_LEVEL = frozenset(
    {
        "summary_schema_version",
        "run_id",
        "run_overview",
        "strategy_behavior",
        "guru_vs_us",
        "execution_quality",
        "capital_deployment",
        "risk_impact",
        "anomalies",
        "token_breakdown",
        "config_fingerprint",
        "pipeline_health",
        "data_quality_flags",
    },
)


class SummaryValidationError(ValueError):
    pass


def validate_summary(doc: dict[str, Any]) -> None:
    """Raise if ``doc`` is missing required v1 sections."""
    missing = _SUMMARY_TOP_LEVEL - doc.keys()
    if missing:
        raise SummaryValidationError(f"summary missing keys: {sorted(missing)}")
    if int(doc["summary_schema_version"]) != SUMMARY_SCHEMA_VERSION:
        raise SummaryValidationError(
            f"unsupported summary_schema_version {doc.get('summary_schema_version')}",
        )
