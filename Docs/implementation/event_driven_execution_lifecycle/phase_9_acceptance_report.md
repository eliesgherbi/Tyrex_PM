# Phase 9 acceptance report — Non-live validation ladder

```text
phase: 9 — Non-live validation ladder
date: 2026-08-07
head: 28c4d804f0c2927579c35743bfcedb807330d768
venue_mutations_attempted: 0
tiny_live_admitted: false
full_suite: 984 passed
gate: COMPLETE_WITH_EXTERNAL_VALIDATION_BLOCKER
```

## D-08 frozen production deadlines (sealed)

Source: `default_n7_sealed_config().timing` / `config/execution/polymarket_live.yaml`
Fingerprint: `fb03c16c087968e253dd601d11f8985c0577029b3577fef21f01e174155c89f7`
(see also `sealed_config_fingerprint.txt`, `d08_deadlines.json`)

| Field | Frozen value |
|---|---:|
| last_allowed_entry_before_end_s | 180.0 |
| discretionary_exit_cutoff_before_end_s | 120.0 |
| mandatory_flatten_start_before_end_s | 90.0 |
| residual_operator_deadline_before_end_s | 45.0 |
| event_end_safety_buffer_s | 30.0 |
| acknowledgment_timeout_s | 15.0 |
| cancel_recon_budget_s | 10.0 |
| exit_retry_max_attempts | 3 |
| exit_retry_time_budget_ms | 60000 |
| ack_timeout_ms | 15000 |
| submission_ack_timeout_ms | 15000 |
| match_evidence_deadline_ms | 60000 |
| user_stream_reconnect_deadline_ms | 30000 |
| rest_recovery_deadline_ms | 30000 |
| exit_ack_timeout_ms | 15000 |
| settlement_max_wait_s | 45.0 |
| settlement_initial_backoff_s | 0.25 |
| settlement_max_backoff_s | 4.0 |
| manual_intervention_deadline_before_end_s | 45.0 |
| status | FROZEN_FOR_N7 |

## Level results

| Level | Result | Notes |
|---:|---|---|
| 1 Unit/contract matrix | PASS | All 30 scenarios mapped; gap tests added |
| 2 Integrated FakeTransport lifecycle | PASS | `fake_lifecycle_run/result.json` → FLAT_CONFIRMED |
| 3 Incident replay | PASS | Deterministic fixture (historical run absent); old false flat vs new non-PASS |
| 4 OBSERVE/SHADOW regression | PASS | 60 pytest + decision capture; no credentials |
| 5 Two-rollover market-data | **BLOCKED** | Network OK; duration gate not run (~10+ min operator host) |
| 6 Authenticated read-only preflight | PASS | `go_no_go=GO`, mutations=0, `tiny_live_admitted=false` |

## Level 1 — Scenario → test matrix

| # | Scenario | Exact test name |
|---:|---|---|
| 1 | Submit rejected before mutation | `test_rejected_before_mutation_no_session_obligation` |
| 2 | Submit timeout; venue order absent | `test_timeout_after_transport_unknown_session_non_pass` |
| 3 | Submit timeout; venue order exists | `test_timeout_venue_order_exists_remains_unknown_until_recon` |
| 4 | HTTP immediate full match | `test_immediate_full_match_wakes_matched_not_confirmed` |
| 5 | HTTP immediate partial match | `test_immediate_partial_match` |
| 6 | HTTP before WebSocket | `test_http_first_then_stream_identical_stores` |
| 7 | WebSocket before HTTP | `test_stream_first_ws_before_http_retained_and_identical` |
| 8 | Duplicate trade event | `test_duplicate_trade_ids_apply_once` |
| 9 | Repeated/decreasing matched HWM | `test_repeated_and_decreasing_hwm` |
| 10 | Match after historical 3s boundary | `test_delayed_match_beyond_3s_wakes_without_fixed_sleep` |
| 11 | Partial entry remainder terminal/canceled | `test_partial_entry_tracks_remainder_separately` |
| 12 | MATCHED not yet sellable | `test_matched_not_confirmed_sellable_remain_zero` |
| 13 | MATCHED→MINED→CONFIRMED | `test_matched_mined_confirmed_progression` |
| 14 | Settlement retry | `test_settlement_retry_failure_does_not_clear_exposure` |
| 15 | Settlement failure | `test_settlement_failure_non_pass_preserves_matched` |
| 16 | Confirmed inventory lagging balance | `test_confirmed_balance_zero_exit_pending` |
| 17 | Partial exit | `test_partial_exit_and_dust_classification` |
| 18 | Duplicate exit execution | `test_duplicate_exit_execution_idempotent` |
| 19 | Book changes before submission | `test_stale_candidate_blocked_before_mutation` |
| 20 | Window changes before submission | `test_window_change_blocked` |
| 21 | User-stream disconnect during fill | `test_disconnect_blocks_new_entry_and_emits_recovery` |
| 22 | Reconnect misses trade; REST repairs | `test_missing_local_fill_auto_repaired_for_owned_trade` |
| 23 | Restart with open order/obligation | `test_restart_reloads_and_blocks_entry` |
| 24 | Venue inventory without local fill | `test_missing_local_fill_auto_repaired_for_owned_trade` |
| 25 | Unexpected account exposure | `test_unexpected_exposure_blocks_and_manual` |
| 26 | Confirmed no-fill | `test_confirmed_no_fill_requires_all_seven_conditions` |
| 27 | Authoritative flat | `test_authoritative_flat_confirmed` |
| 28 | False-flat incident replay | `test_incident_replay_false_flat_never_pass` |
| 29 | Reporting contradiction | `test_reporting_rejects_pass_while_recon_unresolved` |
| 30 | No real mutation in tests | `test_no_real_venue_mutation_in_phase9_ladder` |

Matrix assertion: `tests/test_n7_lifecycle_acceptance.py::test_phase9_scenario_matrix_maps_all_thirty`

## Level 2 — FakeTransport lifecycle

Path exercised (no Portfolio fill injection):

```text
try_enter → response match → apply_settlement_update(CONFIRMED)
→ exit supervisor → run_bounded_exit_ladder → exit CONFIRMED
→ baseline recon → FLAT_CONFIRMED
```

Evidence: `fake_lifecycle_run/result.json`

## Level 3 — Incident replay

- Historical `var/runs/yaml_live_20260731T143127Z` **absent** on this host.
- Deterministic fixture: `tests/fixtures/execution/incident_yaml_live_20260731T143127Z.json`
- Old classifier: false PASS from empty local portfolio after mutation.
- New classifier: non-PASS with open obligation / unresolved recon.
- Evidence: `incident_replay_result.json`

## Level 4 — OBSERVE/SHADOW

- Canonical fixture OBSERVE + SHADOW + resolution suite: **60 passed**
- Decision capture: `observe_shadow_regression/regression_summary.json`
- No credentials required; mutations = 0

## Level 5 — Two-rollover (blocker)

- Public read probes: OK
- Two-rollover session: **not executed** (duration gate)
- Exact blocker + operator command: `two_rollover_market_data/blocker_note.json`

## Level 6 — Authenticated read-only preflight

- Result: **GO** (`authenticated_readonly_preflight/n7_preflight_summary.json`)
- `mutations_enabled=false`, `real_venue_mutations=0`, `tiny_live_admitted=false`
- Historical positions acknowledged, not strategy-owned
- Mutation transport remained unarmed

## Evidence package

```text
phase_9_acceptance_report.md
full_test_result.txt
incident_replay_result.json
fake_lifecycle_run/
observe_shadow_regression/
two_rollover_market_data/
authenticated_readonly_preflight/
sealed_config_fingerprint.txt
tiny_live_admission_checklist.md
```

## Admission

`tiny_live_admitted` remains **false**. Phase 10 is not authorized.
Complete Level 5 on an operator host, re-review this package, then perform the
explicit release action — do not flip admission from this Phase 9 agent run.
