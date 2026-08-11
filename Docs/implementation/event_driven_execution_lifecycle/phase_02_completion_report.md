# Phase 2 completion report — Execution obligations

```text
phase: 2 — Execution obligations
starting_head: 28c4d804f0c2927579c35743bfcedb807330d768
ending_head: 28c4d804f0c2927579c35743bfcedb807330d768  # uncommitted working tree
files_changed:
  - src/tyrex_pm/execution/polymarket/execution_obligation.py (new)
  - src/tyrex_pm/persistence/lifecycle_state.py (new)
  - src/tyrex_pm/persistence/__init__.py
  - src/tyrex_pm/execution/polymarket/live_oms.py
  - src/tyrex_pm/runtime/n7_terminal.py (new)
  - src/tyrex_pm/runtime/n7_admission.py (new)
  - src/tyrex_pm/runtime/n7_live_session.py
  - src/tyrex_pm/runtime/n7_oneshot_host.py
  - src/tyrex_pm/runtime/n7_sealed.py
  - src/tyrex_pm/runtime/n7_timing.py
  - src/tyrex_pm/runtime/n7_abort.py
  - src/tyrex_pm/runtime/yaml_config/resolve.py
  - src/tyrex_pm/reporting/adapters.py
  - src/tyrex_pm/execution/fills_ledger.py (compat shim)
  - config/execution/polymarket_live.yaml
  - tests/helpers_n7.py
  - tests/test_exec_lifecycle_obligations.py (new)
  - tests/test_n7_terminal_safety.py (new)
  - Docs/implementation/event_driven_execution_lifecycle/phase_02_completion_report.md (new)
contracts_added_or_changed:
  - ObligationState / OrderExecutionObligation / SessionExposureObligation / ObligationRegistry
  - LifecycleRuntimeStateStore schema_version=2 (path var/runtime_state/n7/<run_id>/lifecycle_state.json)
  - N7TerminalOutcome + classify_n7_terminal (PASS_N7_ONE_SHOT_FLAT display alias only for FLAT_CONFIRMED)
  - N7AdmissionState: lifecycle_safety_invariants_present=true, tiny_live_admitted=false
  - N7TimingFreeze deadline fields (submission_ack / match_evidence / stream reconnect / rest recovery / settlement / exit_ack / manual intervention)
  - LiveOMS single-writer obligation open as SUBMITTING immediately before transport.submit_order
tests_added:
  - tests/test_exec_lifecycle_obligations.py
  - tests/test_n7_terminal_safety.py
targeted_test_result: 17 passed
full_suite_result: 874 passed, 3 skipped
venue_mutations_attempted: 0
evidence_artifacts:
  - Docs/implementation/event_driven_execution_lifecycle/phase_02_completion_report.md
deviations_from_plan:
  - Lifecycle store attached only when caller provides LifecycleRuntimeStateStore (n7_live_session / tests); FakeTransport hosts keep in-memory obligations by default to avoid shared var/ pollution.
  - After-dispatch clear venue reject resolves order obligation as FAILED (distinct from UNKNOWN); session remains open until Phase 8 positive evidence (conservative).
remaining_blockers:
  - D-08 exact production deadlines remain OPEN_BLOCKING_PHASE_9 (schema defined; values provisional).
  - tiny_live_admitted remains false until Phase 9.
  - NO_FILL_CONFIRMED / FLAT_CONFIRMED require Phase 8 positive venue evidence (placeholders only).
  - D-09 durable lifecycle schema recorded here as schema_version=2.
gate: PASS
```

## Scenario → test mapping

| Required scenario | Test |
|---|---|
| Crash/exception before transport: no mutation; obligation failed/canceled | `test_crash_before_transport_obligation_failed_no_mutation` |
| Timeout after transport: UNKNOWN; session non-PASS | `test_timeout_after_transport_unknown_session_non_pass` |
| Accepted order, zero local fills: obligation open; non-PASS | `test_accepted_zero_fills_obligation_open_non_pass` |
| Empty portfolio + open obligation: never flat PASS | `test_empty_portfolio_open_obligation_never_flat_pass` |
| Exit submission creates its own obligation | `test_exit_submission_creates_own_obligation` |
| Restart reloads obligations and blocks new entry | `test_restart_reloads_and_blocks_entry` |
| Rejected before mutation (no session) | `test_rejected_before_mutation_no_session_obligation` |
| Incompatible schema blocks load | `test_incompatible_schema_blocks_load` |
| Admission / deadline schema | `test_lifecycle_safety_capability_present_admission_default_false`, `test_deadline_fields_present_in_sealed_timing`, `test_invalid_deadline_rejected` |
| Contradiction reporting | `test_contradiction_event_emitted_when_flat_claimed` |
| OBSERVE/SHADOW unchanged | full suite green (existing observe/shadow tests) |

## Deadline schema values (Phase 2 provisional)

| Field | Default | Derivation |
|---|---|---|
| `submission_ack_timeout_ms` | 15000 | `ack_timeout_ms` |
| `match_evidence_deadline_ms` | 60000 | provisional (ack budget × ~4) |
| `user_stream_reconnect_deadline_ms` | 30000 | provisional |
| `rest_recovery_deadline_ms` | 30000 | provisional |
| `exit_ack_timeout_ms` | 15000 | `ack_timeout_ms` |
| `exit_retry_time_budget_ms` | 60000 | existing N7 |
| `settlement_max_wait_s` | 45.0 | R7 `SETTLEMENT_MAX_WAIT_S` |
| `settlement_initial_backoff_s` | 0.25 | R7 |
| `settlement_max_backoff_s` | 4.0 | R7 |
| `manual_intervention_deadline_before_end_s` | 45.0 | `residual_operator_deadline_before_end_s` |

Live admission (`tiny_live_admitted`) remains **false**. Real venue mutations were not attempted.

## Exit gate

```text
PASS — no execution path can produce PASS_N7_ONE_SHOT_FLAT / FLAT_CONFIRMED
after a possible venue mutation unless obligations are resolved by positive
evidence (Phase 8 placeholders). Real venue mutations remain disabled.
```
