"""Declared decision-record fields of settlement_value (recording framework hook).

The driver builds its row once per evaluation; the host records exactly these fields
plus the generic host fields (``recording.records.HOST_FIELDS``). A new row field must be
declared here (and the version bumped) or it is reported as undeclared in the recording
status instead of being recorded silently.
"""

from __future__ import annotations

from tyrex_pm.recording.records import RecordField, RecordSchema

DECISION_RECORD_SCHEMA = RecordSchema(
    name="settlement_value_decision",
    version=2,  # v1 = settlement_decision_record_v1 (replay/observation rows)
    fields=(
    RecordField("aligned_reference", "number"),
    RecordField("attempt_seq", "number"),
    RecordField("backend", "text"),
    RecordField("blockers", "json"),
    RecordField("book_implied_up", "number"),
    RecordField("both_sides_feasible", "bool"),
    RecordField("chosen_side", "text"),
    RecordField("disagreement_gap_pp", "number"),
    RecordField("disagreement_mid_up", "number"),
    RecordField("disagreement_reason", "text"),
    RecordField("down_ask", "number"),
    RecordField("down_bid", "number"),
    RecordField("down_binding", "text"),
    RecordField("down_debit", "number"),
    RecordField("down_ev_conservative", "number"),
    RecordField("down_ev_point", "number"),
    RecordField("down_feasible", "bool"),
    RecordField("down_first_level_edge", "number"),
    RecordField("down_gross_shares", "number"),
    RecordField("down_p_conservative", "number"),
    RecordField("down_reason", "text"),
    RecordField("endpoint_p_up", "number"),
    RecordField("endpoint_p_up_high", "number"),
    RecordField("endpoint_p_up_low", "number"),
    RecordField("episode_phase", "text"),
    RecordField("episode_seq", "number"),
    RecordField("ev_conservative", "number"),
    RecordField("ev_point", "number"),
    RecordField("exit_hold_value", "number"),
    RecordField("exit_intent_id", "text"),
    RecordField("exit_min_price", "number"),
    RecordField("fee_provenance", "text"),
    RecordField("filled_episodes", "number"),
    RecordField("held_p_point", "number"),
    RecordField("exit_trend", "json"),
    RecordField("held_side", "text"),
    RecordField("intended_debit", "number"),
    RecordField("intended_gross_shares", "number"),
    RecordField("intent_id", "text"),
    RecordField("interval_method", "text"),
    RecordField("k", "number"),
    RecordField("k_quality", "text"),
    RecordField("limit_bound", "text"),
    RecordField("max_price", "number"),
    RecordField("observed_at", "text"),
    RecordField("p_up_high", "number"),
    RecordField("p_up_low", "number"),
    RecordField("p_up_point", "number"),
    RecordField("position_quantity", "number"),
    RecordField("reference_price", "number"),
    RecordField("reference_series", "text"),
    RecordField("settlement_functional", "text"),
    RecordField("settlement_provenance", "text"),
    RecordField("sigma", "number"),
    RecordField("sigma_raw", "number"),
    RecordField("spot_input", "text"),
    RecordField("target_feasible", "bool"),
    RecordField("target_mode", "text"),
    RecordField("trading_reference_raw", "number"),
    RecordField("trigger", "text"),
    RecordField("twap_basis_usd", "number"),
    RecordField("twap_mean", "number"),
    RecordField("twap_p_up", "number"),
    RecordField("twap_p_up_high", "number"),
    RecordField("twap_p_up_low", "number"),
    RecordField("twap_reason", "text"),
    RecordField("twap_sd", "number"),
    RecordField("up_ask", "number"),
    RecordField("up_bid", "number"),
    RecordField("up_binding", "text"),
    RecordField("up_debit", "number"),
    RecordField("up_ev_conservative", "number"),
    RecordField("up_ev_point", "number"),
    RecordField("up_feasible", "bool"),
    RecordField("up_first_level_edge", "number"),
    RecordField("up_gross_shares", "number"),
    RecordField("up_p_conservative", "number"),
    RecordField("up_reason", "text"),
    RecordField("variance_time_policy", "text"),
    RecordField("window_attempts", "number"),
    RecordField("worst_walked_price", "number"),
    RecordField("z", "number"),
    ),
)


def decision_record_schema(_config: object) -> RecordSchema:
    return DECISION_RECORD_SCHEMA
