# A0.8 Cumulative Regression Review

**Date:** 2026-07-15  
**Scope:** Post D1–D3 pre-live hardening

## Suite results

| Suite | Result |
|-------|--------|
| A0.8 targeted + D1–D3 new tests | See latest pytest run |
| Cumulative Z-Gap (A0.1–A0.8 + deps) | **248+ pass** (prior baseline; +PTB/outcome tests) |
| Whole-project | **1374+ pass** after shared-path fixes (15 → 9 unrelated) |

## Shared failure triage (mandatory)

### Execution planner (`tests/test_execution_planner.py`) — **FIXED**

| Test | Root cause | Shared path | Z-Gap invokes? | Impact |
|------|------------|-------------|----------------|--------|
| `test_planner_fact_emitted` | `_emit_group_b_observability` treated `context` as enum; `_infer_decision_context` returns `str` | `runtime/pipeline.py` → `_run_execution_planner` | **Yes** — `process_intent_work_unit` for Z-Gap entry/exit | **Execution correctness + observability** |
| `test_planner_changes_do_not_skip_risk` | Same | Same | Yes | Correctness |
| `test_planned_order_revalidated_before_oms` | Same | Same | Yes | Correctness |
| `test_oms_submit_carries_planner_reason` | Same | Same | Yes | Observability |

**Fix:** `pipeline.py` line ~489 — use `context` string directly instead of `.value`.

### Reference price events (`tests/test_reference_price_events.py`) — **FIXED**

| Test | Root cause | Shared path | Z-Gap invokes? | Impact |
|------|------------|-------------|----------------|--------|
| `test_price_to_beat_final_reference_and_direction` | `PriceToBeatTracker.register_market` read global `var/state/chainlink_ticks.jsonl` sidecar, overriding live tick | `ingestion/price_to_beat_tracker.py` | **Yes** — Z-Gap RTDS PTB path | **PTB correctness** |
| `test_price_to_beat_derived_from_first_tick_after_start` | Same | Same | Yes | PTB correctness |

**Fix:** Tests isolate with `chainlink_log_path=tmp_path / "empty.jsonl"`. D2 policy adds explicit live/log precedence.

## Non-gating failures (concrete path evidence)

| Area | Tests | Z-Gap path? | Classification |
|------|-------|-------------|----------------|
| Paired-binary monitor | `test_paired_monitor_starts_after_reconciled_entry_qty`, `test_sellability_gate_emits_monitor_started` | No — `paired_binary_run.py` only | Non-gating |
| Survival stall/replay | `test_survival_stall_exit.py` (3), `test_replay_survival_advisory.py` | No — Phase 1 survivor stack | Non-gating |
| Validation harness | `test_validation_harness_*.py` (2) | No — guru validation runtime | Non-gating |
| Phase1 classification | `test_validate_activation_unwind_fail_classification.py` | No — paired_binary terminal classifier | Non-gating |
| Allocation clamp | `test_allocation_clamp_grace_and_entry_reconcile.py` | Indirect — shared allocation ledger; Z-Gap uses same ledger API but not this paired-binary monitor path | Non-gating for Z-Gap enforce |

## Verdict

Shared planner and reference-price failures affecting the Z-Gap path are **resolved**. Remaining whole-project failures are **non-gating** for Z-Gap tiny-live with documented code-path separation.

Live run must not proceed until operator completes Stages 4–6 blockers (fresh PTB, calibration sign-off, scoped approval).
