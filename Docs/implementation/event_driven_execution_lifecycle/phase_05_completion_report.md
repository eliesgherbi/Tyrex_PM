# Phase 5 completion report — Exposure-driven exit supervisor

```text
phase: 5 — Exposure-driven exit supervisor
starting_head: 28c4d804f0c2927579c35743bfcedb807330d768
ending_head: 28c4d804f0c2927579c35743bfcedb807330d768 (uncommitted working tree; no commit per operator request)
files_changed:
  - src/tyrex_pm/runtime/exit_supervisor.py (new)
  - src/tyrex_pm/runtime/n7_live_session.py
  - src/tyrex_pm/runtime/n7_oneshot_host.py
  - src/tyrex_pm/execution/polymarket/live_oms.py
  - src/tyrex_pm/execution/order_store.py (defensive get(str|OrderId))
  - tests/test_exec_lifecycle_exit_wake.py (new)
  - Docs/implementation/event_driven_execution_lifecycle/phase_05_completion_report.md (new)
contracts_added_or_changed:
  - ExitSupervisor + event inputs (MatchedExposureChanged, SettlementChanged,
    InventorySellable, BookViewChanged, ExitIntentSignal, TimerDeadline,
    KillSwitchActivated, ReconciliationResult)
  - ExitSupervisorPhase / ExitDecisionAction orchestration state machine
  - LiveOMS.on_matched_exposure wake hook after apply_response_match
  - LifecycleRuntimeStateStore.extra["exit_supervisor"] restart persistence
  - N7OneShotHost.route_exit_intent / notify_inventory_sellable
  - run_exposure_exit_supervision async loop (no fixed-sleep lifecycle control)
  - run_bounded_exit_ladder consults supervisor when MATCHED supervision woken;
    SELL blocked until sellable (Phase 6)
tests_added:
  - tests/test_exec_lifecycle_exit_wake.py (13 tests)
targeted_test_result: 13 passed (test_exec_lifecycle_exit_wake.py)
related_smoke: test_n7_oneshot + test_exec_lifecycle_obligations +
  test_exec_lifecycle_response_match + test_n7_terminal_safety → pass
full_suite_result: 907 passed, 3 skipped
venue_mutations_attempted: 0
evidence_artifacts:
  - Docs/implementation/event_driven_execution_lifecycle/phase_05_completion_report.md
deviations_from_plan:
  - Confirmed-inventory exit paths that never woke MATCHED still execute the
    legacy ladder after routing the intent (preserves pre-Phase-5 FakeTransport
    fill_order tests). MATCHED-woken supervision enforces WAIT_SELLABLE until
    InventorySellable (Phase 6 wires settlement→sellable).
  - Bounded poll sleeps (0.05s) remain inside run_exposure_exit_supervision for
    event scheduling only; they do not determine success.
  - Parallel Phase 4 user_stream_live.py left untouched.
remaining_blockers:
  - Phase 6 required to notify InventorySellable / authorize real SELL qty.
  - Phase 7 continuous feeds / pre-submit revalidation still open.
  - Phase 8 baseline-aware reconciliation still required for FLAT_CONFIRMED.
gate: PASS
```

## Exit-gate evidence

| Requirement | Evidence |
|---|---|
| Matched strategy-owned exposure wakes supervisor | `test_matched_exposure_wakes_supervisor`, `test_live_oms_hook_wakes_supervisor_on_response_match` |
| No fixed sleep controls exit reachability | `test_n7_live_session_has_no_fixed_sleep_lifecycle_control`, `test_delayed_match_beyond_3s_wakes_without_fixed_sleep` |
| Unresolved exposure remains pending / non-PASS | `test_exit_need_pending_when_matched_not_sellable`, `test_unresolved_supervisor_is_non_pass`, `test_host_run_bounded_exit_ladder_pending_when_matched_not_sellable` |
| SELL not submitted merely because matched exists | Host ladder returns `PENDING_SELLABLE`; FakeTransport has no SELL submit |

## SUP work-package mapping

| ID | Status |
|---|---|
| SUP-01 supervisor state + event interface | DONE |
| SUP-02 wake on first positive matched delta | DONE |
| SUP-03 exit need pending when not sellable | DONE |
| SUP-04 route strategy/kill/thesis/time-exit | DONE |
| SUP-05 replace sleep(3)+is_flat in N7 | DONE |
| SUP-06 partial entry / remainder tracking | DONE |
| SUP-07 exit requested/submitted/matched/retry | DONE |
| SUP-08 escalate deadlines; never false success | DONE |
| SUP-09 persist supervisor for restart | DONE |
