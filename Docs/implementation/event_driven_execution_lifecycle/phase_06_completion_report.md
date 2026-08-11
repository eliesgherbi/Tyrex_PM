# Phase 6 completion report — Shared settlement and sellability

```text
phase: 6 — Shared settlement and sellability
starting_head: 28c4d804f0c2927579c35743bfcedb807330d768
ending_head: 28c4d804f0c2927579c35743bfcedb807330d768 (uncommitted working tree; no commit per operator request)
files_changed:
  - src/tyrex_pm/execution/polymarket/settlement.py
    (InventoryAxes, compute_safe_sell_qty)
  - src/tyrex_pm/execution/polymarket/settlement_bridge.py (new)
    SharedSettlementCoordinator — N7-facing reuse of settlement.py + MutationLifecycle
  - src/tyrex_pm/execution/polymarket/normalize.py
    stream_trade_is_confirmed → CONFIRMED only (MINED ≠ inventory)
  - src/tyrex_pm/execution/polymarket/live_oms.py
    apply_confirmed_trade_fill; on_settlement_trade; obligation resolve on CONFIRMED
  - src/tyrex_pm/execution/polymarket/fake_transport.py
    conditional balance helpers for sellability fakes
  - src/tyrex_pm/runtime/n7_oneshot_host.py
    settlement bridge wiring; apply_settlement_update; safe SELL qty in exit ladder
  - src/tyrex_pm/runtime/n7_live_session.py
    settlement tick so WAIT_SELLABLE can become actionable
  - src/tyrex_pm/runtime/exit_supervisor.py
    on_poll_tick hook for settlement refresh
  - tests/test_exec_lifecycle_settlement.py (new)
  - Docs/implementation/event_driven_execution_lifecycle/phase_06_completion_report.md (new)
contracts_added_or_changed:
  - InventoryAxes + compute_safe_sell_qty
    sell_qty = min(confirmed_strategy_owned, funder_conditional_balance, remaining_unexited)
  - SharedSettlementCoordinator (matched / confirmed / sellable projections)
  - MutationPhase driven from MATCHED→MINED→CONFIRMED (+ balance for POSITION_ACTIVE)
  - InventorySellable / SettlementChanged notifications to ExitSupervisor
  - LiveOMS.apply_confirmed_trade_fill (FillLedger idempotent; MINED ignored)
  - Obligations RESOLVED_FILLED only from CONFIRMED fill evidence
tests_added:
  - tests/test_exec_lifecycle_settlement.py (18 tests)
targeted_test_result: 18 passed (test_exec_lifecycle_settlement.py)
related_smoke: exit_wake + user_stream + response_match + obligations + ordering
  + n7_terminal_safety + settlement → 92 passed
full_suite_result: 941 passed, 3 skipped
venue_mutations_attempted: 0
evidence_artifacts:
  - Docs/implementation/event_driven_execution_lifecycle/phase_06_completion_report.md
deviations_from_plan:
  - N7 uses SharedSettlementCoordinator rather than importing R7 one-shot
    orchestration; wait_for_entry_settlement / evaluate_sell_readiness /
    classify_flatness / MutationLifecycle are reused as shared contracts.
  - Live-session settlement refresh is a transport poll tick (apply_settlement_update),
    not a blocking RealSettlementClock wait inside the async supervisor loop.
    advance_settlement_for_exit remains available for injectable pollers/tests.
  - stream_trade_is_confirmed tightened to CONFIRMED-only (was CONFIRMED|MINED);
    required so MATCHED/MINED cannot create portfolio inventory.
remaining_blockers:
  - Phase 7 continuous feeds / pre-submit revalidation still open.
  - Phase 8 baseline-aware reconciliation still required for FLAT_CONFIRMED.
gate: PASS
```

## Exit-gate evidence

| Requirement | Evidence |
|---|---|
| Supervision begins at MATCHED | `test_matched_not_confirmed_supervisor_active_sell_blocked`, Phase 5 wake preserved |
| Confirmed accounting only from confirmed evidence | `test_matched_mined_confirmed_progression`, `test_mined_stream_does_not_create_portfolio_inventory` |
| SELL cannot exceed authenticated sellable inventory | `test_confirmed_gt_balance_sell_capped`, `test_safe_sell_qty_formula_bounds`, `test_fake_full_lifecycle_matched_to_sell` |
| MATCHED must not create confirmed/sellable inventory | three-axis + MATCHED-blocked SELL tests |

## Required-test map (Phase 6)

| Required scenario | Exact name |
|---|---|
| MATCHED not CONFIRMED: supervisor active, SELL blocked | `test_matched_not_confirmed_supervisor_active_sell_blocked` |
| MINED not CONFIRMED: SELL blocked | `test_mined_not_confirmed_sell_blocked` |
| CONFIRMED but balance zero/lagging: exit pending | `test_confirmed_balance_zero_exit_pending` |
| Confirmed > balance: SELL capped to balance | `test_confirmed_gt_balance_sell_capped` |
| Partial confirmed inventory actionable only | `test_partial_confirmed_only_actionable` |
| Settlement failure: non-PASS / exposure preserved | `test_settlement_failure_non_pass_preserves_matched`, `test_settlement_retry_failure_does_not_clear_exposure` |
| Duplicate settlement: exactly-once portfolio | `test_duplicate_settlement_exactly_once_portfolio` |
| Partial exit + dust classification | `test_partial_exit_and_dust_classification` |

## STL work-package mapping

| ID | Status |
|---|---|
| STL-01 separate matched/confirmed/sellable projections | DONE |
| STL-02 feed stream/REST statuses into shared evaluator | DONE |
| STL-03 drive N7 MutationPhase transitions | DONE |
| STL-04 notify supervisor when InventorySellable | DONE |
| STL-05 compute safe SELL quantity | DONE |
| STL-06 route safe qty through exit planner + LiveOMS | DONE |
| STL-07 settlement retry/failure without clearing exposure | DONE |
| STL-08 resolve obligations only from terminal evidence | DONE |
| STL-09 confirmed fills idempotent via FillLedger | DONE |
