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
    "layer_a_filter": frozenset(
        {
            "correlation_id",
            "filter_name",
            "branch",
            "accept",
            "reason_code",
            "detail",
            "metadata",
        },
    ),
    "sizing": frozenset({"correlation_id", "target_qty", "signal_branch"}),
    "risk_decision": frozenset({"correlation_id", "allowed", "reason_code"}),
    "tradable_state_health": frozenset(
        {
            "correlation_id",
            "level",
            "reason_code",
            "observed_at_utc",
            "risk_allowed",
            "risk_reason_code",
        },
    ),
    "startup_readiness": frozenset(
        {
            "status",
            "reasons",
            "timeout_seconds",
            "mode",
            "t0_mono",
            "deadline_mono",
            "terminal",
        },
    ),
    "shutdown_drain": frozenset(
        {
            "skipped",
            "skip_reason",
            "timed_out",
            "residual_count",
            "canceled_count",
            "drain_duration_ms",
            "residual_client_order_ids",
            "instruments_cancelled",
            "cancel_failures",
            "cancel_partial_failure",
            "internal_error",
            "drain_aborted_internal",
        },
    ),
    "execution_alignment_profile": frozenset(
        {
            "polymarket_use_data_api_for_positions",
            "live_exec_open_check_open_only",
        },
    ),
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
    # Harness diagnostics: rows share ``kind``; other keys vary by event (flattened into row).
    "bot_sell_validate": frozenset({"kind"}),
    "wallet_sync": frozenset(
        {
            "cycle",
            "positions_fetched",
            "orders_fetched",
            "condition_ids_wallet",
            "condition_ids_cache",
            "newly_added",
            "resolution_failures",
            "unresolvable_retrying",
            "unresolvable_terminal",
            "http_positions_ok",
            "http_orders_ok",
            "first_sync_complete",
            "elapsed_ms",
            "failure_details",
        },
    ),
    "wallet_sync_startup_timeout": frozenset(
        {
            "cycle",
            "elapsed_since_start_s",
            "deadline_s",
        },
    ),
    "venue_state": frozenset(
        {
            "status",
            "position_count",
            "resting_order_count",
            "cash_ready",
            "ttl_seconds",
            "cash_poll_interval_seconds",
            "last_positions_success_utc",
            "last_cash_success_utc",
        },
    ),
    "venue_state_missing_mark": frozenset(
        {
            "instrument_id",
            "fallback_price",
        },
    ),
    # Virtual TP/SL (Tyrex): payloads vary slightly by phase; only a minimal key set is required.
    "virtual_exit_arm": frozenset({"lot_id", "token_id", "guru_correlation_id"}),
    "virtual_exit_trigger": frozenset(
        {"lot_id", "kind", "executable_price", "trigger_basis"},
    ),
    "virtual_exit_submit": frozenset(
        {
            "lot_id",
            "kind",
            "order_style",
            "qty",
            "correlation_id",
            "intent_origin",
        },
    ),
    "virtual_exit_hold": frozenset({"reason"}),
    "virtual_exit_retry": frozenset({"lot_id", "reason", "attempt"}),
    "virtual_exit_reconcile": frozenset({"lot_id", "reason"}),
    "virtual_exit_disarm": frozenset({"lot_id", "reason"}),
    "virtual_exit_recovery": frozenset({"action"}),
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
