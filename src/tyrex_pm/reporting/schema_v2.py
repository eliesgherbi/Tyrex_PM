from __future__ import annotations

FACT_SCHEMA_VERSION = 2

FACT_TYPE_GURU_SIGNAL = "guru_signal"
#: Generic (non-guru) signal ingress fact (P1 architecture_enhance). Emitted by
#: ``process_signals`` for any signal whose ``source != "guru"`` so non-guru
#: sources are not invisible before ``intent_created``. ``guru_signal`` is kept
#: unchanged for backward compatibility; guru does not emit ``signal_received``.
FACT_TYPE_SIGNAL_RECEIVED = "signal_received"
FACT_TYPE_STRATEGY_SKIP = "strategy_skip"
FACT_TYPE_INTENT = "intent_created"
FACT_TYPE_RISK = "risk_decision"
FACT_TYPE_OMS_SUBMIT = "oms_submit"
FACT_TYPE_OMS_REJECT = "oms_reject"
FACT_TYPE_OMS_CANCEL = "oms_cancel"
FACT_TYPE_OMS_RESULT = "oms_result"
FACT_TYPE_HEALTH = "health"
FACT_TYPE_RECONCILE = "reconcile"
FACT_TYPE_GURU_POLL = "guru_poll"
FACT_TYPE_LIVE_ATTEST = "live_attest"
#: Emitted after a successful REST wallet refresh (open orders + balance/allowance, optionally
#: positions). Lets operators see the cadence and content of the positions/balance safety net
#: without having to infer it from the absence of symptoms. See ``runtime.pipeline.emit_wallet_sync``.
FACT_TYPE_WALLET_SYNC = "wallet_sync"
#: Scheduled exit / sell_test lifecycle (P3.5): pending, arm attempts, terminal SELL outcomes.
FACT_TYPE_EXIT_LIFECYCLE = "exit_lifecycle"
#: Per-strategy token allocation mutations (P4): buy/sell/reserve/clamp.
FACT_TYPE_ALLOCATION_LEDGER = "allocation_ledger"
#: ExecutionPlanner output (P3 architecture_enhance): chosen order style, price,
#: size, urgency, planner reason and book evidence. Final validation reuses
#: ``risk_decision`` with ``{"phase": "planned"}``.
FACT_TYPE_EXECUTION_PLAN = "execution_plan"
#: Protection (TP/SL) overlay facts (P4 architecture_enhance). ``protection_tick``
#: is deduped so periodic monitoring does not flood facts.jsonl.
FACT_TYPE_PROTECTION_REGISTER = "protection_register"
FACT_TYPE_PROTECTION_TICK = "protection_tick"
FACT_TYPE_PROTECTION_TRIGGER = "protection_trigger"

# Paired binary strategy facts (Phase 4.6)
FACT_TYPE_PAIRED_BINARY_ENTRY_EVAL = "paired_binary_entry_eval"
FACT_TYPE_PAIRED_BINARY_ENTRY_SKIP = "paired_binary_entry_skip"
FACT_TYPE_PAIRED_BINARY_ENTRY_SUBMITTED = "paired_binary_entry_submitted"
FACT_TYPE_PAIRED_BINARY_STATE_CHANGE = "paired_binary_state_change"
FACT_TYPE_PAIRED_BINARY_LEG_STOP = "paired_binary_leg_stop"
FACT_TYPE_PAIRED_BINARY_WINNER_TARGET = "paired_binary_winner_target"
FACT_TYPE_PAIRED_BINARY_TIMEOUT_EXIT = "paired_binary_timeout_exit"
FACT_TYPE_PAIRED_BINARY_UNWIND = "paired_binary_unwind"
FACT_TYPE_PAIRED_BINARY_DONE = "paired_binary_done"
FACT_TYPE_PAIRED_BINARY_RECOVERED = "paired_binary_recovered"
FACT_TYPE_PAIRED_BINARY_ENTRY_QTY_RECONCILED = "paired_binary_entry_qty_reconciled"
FACT_TYPE_PAIRED_BINARY_ENTRY_TIMEOUT_UNWIND = "paired_binary_entry_timeout_unwind"
FACT_TYPE_PAIRED_BINARY_MONITOR_STARTED = "paired_binary_monitor_started"
FACT_TYPE_PAIRED_BINARY_WAITING_SELLABLE = "paired_binary_waiting_for_sellable_inventory"
FACT_TYPE_PAIRED_BINARY_ACTIVATION_REFERENCE = "paired_binary_activation_reference"
FACT_TYPE_PAIRED_BINARY_EXIT_TRIGGER_PENDING = "paired_binary_exit_trigger_pending"
FACT_TYPE_PAIRED_BINARY_EXIT_SUBMIT_ATTEMPT = "paired_binary_exit_submit_attempt"
FACT_TYPE_PAIRED_BINARY_EXIT_SUBMIT_BLOCKED = "paired_binary_exit_submit_blocked"
FACT_TYPE_PAIRED_BINARY_EXIT_RETRY = "paired_binary_exit_retry"
FACT_TYPE_PAIRED_BINARY_EXIT_STATE_RECOVERED = "paired_binary_exit_state_recovered"
FACT_TYPE_PAIRED_BINARY_PNL_PLAN = "paired_binary_pnl_plan"
FACT_TYPE_PAIRED_BINARY_STOP_PLAN = "paired_binary_stop_plan"
FACT_TYPE_PAIRED_BINARY_WINNER_TARGET_PLAN = "paired_binary_winner_target_plan"
FACT_TYPE_PAIRED_BINARY_WINNER_TARGET_REPRICED = "paired_binary_winner_target_repriced"
FACT_TYPE_PAIRED_BINARY_REALIZED_PNL = "paired_binary_realized_pnl"
FACT_TYPE_PAIRED_BINARY_REALIZED_PNL_UNAVAILABLE = "paired_binary_realized_pnl_unavailable"
FACT_TYPE_PAIRED_BINARY_PRICE_BASED_PNL_ESTIMATE = "paired_binary_price_based_pnl_estimate"
FACT_TYPE_PAIRED_BINARY_ACTIVATION_REJECTED_LOSS_BUDGET = "paired_binary_activation_rejected_loss_budget"
FACT_TYPE_PAIRED_BINARY_ACTIVATION_GAP_RECHECK = "paired_binary_activation_gap_recheck"
FACT_TYPE_PAIRED_BINARY_ACTIVATION_RECOVERED = "paired_binary_activation_recovered"
FACT_TYPE_PAIRED_BINARY_ENTRY_PRICE_UNKNOWN = "paired_binary_entry_price_unknown"
FACT_TYPE_PAIRED_BINARY_ENTRY_PRICE_MISMATCH = "paired_binary_entry_price_mismatch"
FACT_TYPE_PAIRED_BINARY_EMERGENCY_UNWIND_STARTED = "paired_binary_emergency_unwind_started"
FACT_TYPE_PAIRED_BINARY_EMERGENCY_UNWIND_ATTEMPT = "paired_binary_emergency_unwind_attempt"
FACT_TYPE_PAIRED_BINARY_EMERGENCY_UNWIND_BLOCKED = "paired_binary_emergency_unwind_blocked"
FACT_TYPE_PAIRED_BINARY_EMERGENCY_UNWIND_RETRY = "paired_binary_emergency_unwind_retry"
FACT_TYPE_PAIRED_BINARY_EMERGENCY_UNWIND_DONE = "paired_binary_emergency_unwind_done"
FACT_TYPE_PAIRED_BINARY_MANUAL_INTERVENTION_REQUIRED = "paired_binary_manual_intervention_required"
FACT_TYPE_PAIRED_BINARY_LATENCY_SAMPLE = "paired_binary_latency_sample"
FACT_TYPE_PAIRED_BINARY_BOOK_CAPTURE_QUALITY = "paired_binary_book_capture_quality"
FACT_TYPE_PAIRED_BINARY_PAIR_PREFLIGHT = "paired_binary_pair_preflight"
FACT_TYPE_PAIRED_BINARY_PAIR_PREFLIGHT_REJECTED = "paired_binary_pair_preflight_rejected"
FACT_TYPE_PAIRED_BINARY_ENTRY_ORDER_STYLE_APPLIED = "paired_binary_entry_order_style_applied"
FACT_TYPE_PAIRED_BINARY_ENTRY_LEG_BLOCKED = "paired_binary_entry_leg_blocked"
FACT_TYPE_PAIRED_BINARY_ENTRY_ASYMMETRY_DETECTED = "paired_binary_entry_asymmetry_detected"
FACT_TYPE_PAIRED_BINARY_ENTRY_CANCEL_ATTEMPT = "paired_binary_entry_cancel_attempt"
FACT_TYPE_PAIRED_BINARY_ENTRY_CANCEL_ACK = "paired_binary_entry_cancel_ack"
FACT_TYPE_PAIRED_BINARY_ENTRY_CANCEL_FAILED = "paired_binary_entry_cancel_failed"
FACT_TYPE_PAIRED_BINARY_PAIR_ENTRY_COMMITTED = "paired_binary_pair_entry_committed"
FACT_TYPE_PAIRED_BINARY_PAIR_ENTRY_ABORTED_FLAT = "paired_binary_pair_entry_aborted_flat"
FACT_TYPE_PAIRED_BINARY_PAIR_ENTRY_MANUAL_INTERVENTION = "paired_binary_pair_entry_manual_intervention"
FACT_TYPE_PAIRED_BINARY_ENTRY_TIMEOUT_UNWIND_RETRY = "paired_binary_entry_timeout_unwind_retry"

# Phase 2 market WS shadow facts (M1)
FACT_TYPE_MARKET_WS_CONNECTED = "market_ws_connected"
FACT_TYPE_MARKET_WS_DISCONNECTED = "market_ws_disconnected"
FACT_TYPE_WS_VS_REST_BOOK_COMPARE = "ws_vs_rest_book_compare"
FACT_TYPE_OUT_OF_ORDER_EVENT = "out_of_order_event"
FACT_TYPE_WS_SEQUENCE_GAP_DETECTED = "ws_sequence_gap_detected"

# Phase 2 Group B observability (M3/M4/M7)
FACT_TYPE_DATA_QUALITY_VERDICT = "data_quality_verdict"
FACT_TYPE_EXECUTION_PLANNER_EVIDENCE = "execution_planner_evidence"
FACT_TYPE_DECISION_SNAPSHOT = "decision_snapshot"
FACT_TYPE_LATENCY_CHAIN = "latency_chain"
FACT_TYPE_MARKET_READINESS_TRANSITION = "market_readiness_transition"
FACT_TYPE_MARKET_DATA_HEALTH_BLOCK = "market_data_health_block"
FACT_TYPE_PAIRED_BINARY_TICK_SOURCE = "paired_binary_tick_source"
FACT_TYPE_REST_POLL_DISABLED = "rest_poll_disabled"
FACT_TYPE_WS_PRIMARY_CUTOVER = "ws_primary_cutover"

# Required payload keys per fact type (schema validation for tests).
FACT_PAYLOAD_SCHEMA: dict[str, tuple[str, ...]] = {
    FACT_TYPE_DATA_QUALITY_VERDICT: (
        "decision_id",
        "decision_context",
        "verdict",
        "reasons",
        "profile_id",
    ),
    FACT_TYPE_EXECUTION_PLANNER_EVIDENCE: (
        "decision_id",
        "snapshot_id",
        "book_age_ms",
        "source",
        "quality_verdict",
    ),
    FACT_TYPE_DECISION_SNAPSHOT: ("decision_id", "decision_type", "snapshot_ids"),
    FACT_TYPE_LATENCY_CHAIN: ("decision_id",),
}


def validate_fact_payload(fact_type: str, payload: dict) -> list[str]:
    """Return missing required keys for ``fact_type`` (empty if valid)."""
    required = FACT_PAYLOAD_SCHEMA.get(fact_type)
    if required is None:
        return []
    return [k for k in required if k not in payload]

# Canonical key inside oms_submit / oms_cancel payloads for venue response summary string.
OMS_RESULT_PAYLOAD_KEY = "oms_result"
