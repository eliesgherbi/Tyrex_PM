"""Per-fact validation (SCH-02)."""

from __future__ import annotations

from typing import Any

from tyrex_pm.reporting.versioning import REPORTING_FACT_SCHEMA_VERSION

# fact_type -> required payload keys (beyond envelope)
_REQUIRED: dict[str, frozenset[str]] = {
    "run_manifest": frozenset(
        {
            "run_id",
            "started_at_utc",
            "trader_id",
            "execution_mode",
            "guru_ingest_mode",
            "execution_path",
        },
    ),
    "config_snapshot": frozenset({"run_id", "config_sha256", "config_json"}),
    "guru_signal": frozenset(
        {
            "correlation_id",
            "source",
            "side",
            "token_id",
            "ts_event_ms",
            "ts_emit_ms",
            "guru_size_raw",
            "guru_price_raw",
        },
    ),
    "guru_shadow_compare": frozenset(
        {
            "correlation_id",
            "side",
            "token_id",
            "ts_event_ms",
            "ts_recv_ms",
            "would_publish_new",
        },
    ),
    "health_anomaly": frozenset({"component", "event_type"}),
    "strategy_decision": frozenset({"correlation_id", "branch", "decision", "reason_code"}),
    "sizing": frozenset({"correlation_id", "target_qty", "signal_branch"}),
    "risk_decision": frozenset({"correlation_id", "allowed", "reason_code"}),
    "execution_intent": frozenset({"correlation_id", "token_id", "side", "quantity", "signal_kind"}),
    "normalization": frozenset(
        {
            "correlation_id",
            "skipped_submit",
            "reason_code",
            "pre_qty",
            "post_qty",
            "pre_price",
            "post_price",
        },
    ),
    "book_constraint": frozenset({"correlation_id", "book_source"}),
    "execution_outcome": frozenset(
        {
            "correlation_id",
            "outcome",
            "reason_code",
            "instrument_id",
            "submitted_qty",
            "submitted_price",
        },
    ),
    "order_correlation_map": frozenset({"correlation_id", "client_order_id", "instrument_id"}),
    "order_lifecycle": frozenset({"client_order_id", "status"}),
    "fill": frozenset({"client_order_id", "fill_event_id"}),
    "account_snapshot": frozenset(
        {
            "account_snapshot_seq",
            "account_present",
            "snapshot_trigger",
            "captured_at_utc",
        },
    ),
    "deployment_budget": frozenset(
        {
            "correlation_id",
            "order_deploy_usd",
            "token_pending_usd",
            "token_filled_usd",
            "token_deploy_usd",
            "portfolio_pending_usd",
            "portfolio_filled_usd",
            "portfolio_deploy_usd",
        },
    ),
    "position": frozenset({"instrument_id"}),
    "component_status": frozenset({"component", "status"}),
    "report_pipeline_health": frozenset({"flush_ok"}),
    "reconciliation": frozenset({"check_type", "outcome"}),
}


class FactValidationError(ValueError):
    pass


def validate_fact_row(row: dict[str, Any]) -> None:
    """Validate a full JSONL row (including envelope fields)."""
    for k in ("fact_type", "run_id", "fact_schema_version", "recorded_at_utc"):
        if k not in row:
            raise FactValidationError(f"missing envelope field: {k}")
    ft = str(row["fact_type"])
    if ft not in _REQUIRED:
        raise FactValidationError(f"unknown fact_type: {ft}")
    req = _REQUIRED[ft]
    missing = req - row.keys()
    if missing:
        raise FactValidationError(f"fact_type={ft} missing keys: {sorted(missing)}")
    if int(row["fact_schema_version"]) != REPORTING_FACT_SCHEMA_VERSION:
        raise FactValidationError(
            f"unsupported fact_schema_version {row['fact_schema_version']} "
            f"(expected {REPORTING_FACT_SCHEMA_VERSION})",
        )


def fact_envelope(
    *,
    fact_type: str,
    run_id: str,
    recorded_at_utc: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "fact_schema_version": REPORTING_FACT_SCHEMA_VERSION,
        "fact_type": fact_type,
        "run_id": run_id,
        "recorded_at_utc": recorded_at_utc,
        **payload,
    }
    validate_fact_row(row)
    return row
