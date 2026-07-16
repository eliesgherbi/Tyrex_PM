# Milestone 7 — Observability, validator, replay, and live protocol

**Program:** [phase1.md](phase1.md) §11–§13, Appendix B  
**Status:** specification — not implemented  
**Depends on:** Milestones 0–6 (evolves throughout; final acceptance after all)

---

## 1. Purpose

Consolidate Phase 1 **facts**, **validator classifications**, **replay tooling**, and **tiny-live protocol** so survival behavior is debuggable and regression-tested against Phase 2 baseline.

---

## 2. Why this milestone exists

Implementation across M0–M6 is useless without consistent observability and proof that `survival.enabled: false` preserves Phase 2. M7 delivers the validation harness operators will use before enabling `enforce` modes.

---

## 3. Scope

- `reporting/schema_v2.py` — all Phase 1 fact constants
- Fact emitters — centralized evidence payload helper
- `scripts/validate_paired_binary_phase2_live_run.py` — extend classifications
- `scripts/analyze_live_runs_review.py` — survival + replay summary
- `config/scenarios/live_paired_binary_phase1_tiny.yaml` — overlay scenario
- `tests/test_paired_binary_phase2_regression.py` — full regression suite
- `tests/test_paired_binary_survival_monitor.py` — integration
- Offline replay script (new or extend analyze script)

---

## 4. Non-goals

- Live trading execution in CI
- Changing Phase 2 classifications when survival disabled
- ML replay / prediction

---

## 5. Current code areas to review

| File | Notes |
|------|-------|
| `reporting/schema_v2.py` | Existing paired_binary facts |
| `strategies/paired_binary/facts.py` | Emitter patterns, `base_payload` |
| `scripts/validate_paired_binary_phase2_live_run.py` | PHASE2_* classifications |
| `scripts/analyze_live_runs_review.py` | Run aggregation |
| `var/reporting/live_runs_review.json` | Golden replay inputs |

---

## 6. Target architecture

```text
survival/facts.py (or reporting/survival_facts.py)
  build_survival_evidence_payload(...) → dict  # standard fields

validate_paired_binary_phase1_live_run.py (extend phase2 script or sibling)
  classify PHASE1_SURVIVAL_PASS | PHASE1_RUNTIME_PREMATURE_EXIT | PHASE2_* unchanged

scripts/replay_survival_advisory.py
  load facts.jsonl → run target/reachability/stall offline → summary JSON
```

---

## 7. Detailed implementation plan

### 7.1 Files to create

| File | Purpose |
|------|---------|
| `src/tyrex_pm/survival/facts.py` | Shared evidence builder + emit helpers |
| `config/scenarios/live_paired_binary_phase1_tiny.yaml` | Phase 1 tiny overlay |
| `scripts/replay_survival_advisory.py` | Offline replay live1/3/5 |
| `tests/test_paired_binary_survival_monitor.py` | Integration |
| `tests/test_paired_binary_phase2_regression.py` | If not created in M0 |
| `tests/test_validate_phase1_classifications.py` | Validator unit tests (optional) |

### 7.2 Files to modify

| File | Changes |
|------|---------|
| `reporting/schema_v2.py` | All fact constants below |
| `strategies/paired_binary/facts.py` | Delegate to survival facts or re-export |
| `scripts/validate_paired_binary_phase2_live_run.py` | Phase 1 classifications |
| `scripts/analyze_live_runs_review.py` | Survival summary fields |
| `Docs/OPERATIONS.md` | Tiny-live protocol (optional, only if user wants — skip unless exists) |

### 7.3 Fact types (complete list)

```text
survivor_target_selected
survivor_target_downgraded
survivor_target_unreachable
survivor_target_impossible
survivor_reachability_scored
survivor_progress_evaluated
survivor_stall_detected
survivor_executable_exit_evaluated
survivor_trailing_stop_armed
survivor_trailing_stop_triggered
survivor_economics_evaluated
survivor_early_exit_triggered
strategy_runtime_decision
strategy_runtime_fallback_max_runtime
strategy_lifecycle_entry_blocked
strategy_lifecycle_pre_close_flatten_required
kill_switch_triggered
```

### 7.4 Standard evidence payload (`build_survival_evidence_payload`)

Every survival fact must include where applicable:

```text
selected_target, target_mode, current_executable_bid, touch_bid, sweep_vwap,
depth_fraction, seconds_to_close, elapsed_since_loser_exit, progress_ratio,
reachability_verdict, economics_verdict, decision_action, enforcement_mode,
snapshot_id, planner_evidence_ref
```

Implement once; all M1–M6 emitters call it.

### 7.5 Validator classifications

**Add:**

| Classification | Criteria |
|----------------|----------|
| `PHASE1_SURVIVAL_PASS` | survival enabled; lifecycle or survival exit completes; no premature fallback-runtime flatten; expected facts present |
| `PHASE1_RUNTIME_PREMATURE_EXIT` | open survivor flattened before `event_end_ts - flatten_before_event_end_s` due to old max_runtime tick budget |

**Unchanged when `survival.enabled: false`:**

```text
PHASE2_WS_LIFECYCLE_PASS
PHASE2_WS_OPEN_EXPOSURE_FORCE_FLATTEN_PASS
PHASE2_WS_OBSERVATION_PASS
PHASE2_WS_FAIL
```

Detect survival disabled from manifest/scenario or absence of survival facts + default config.

### 7.6 Replay protocol

```bash
python scripts/replay_survival_advisory.py \
  --facts var/reporting/runs/paired_binary_ws_primary_live3_*/facts.jsonl \
  --config config/scenarios/live_paired_binary_phase1_tiny.yaml
```

Output: JSON summary — what reachability/stall/target policy **would** have advised vs actual OMS outcomes. No OMS calls.

Replay inputs: **live1, live3, live5** from `var/reporting/live_runs_review.json`.

### 7.7 Tiny-live protocol

1. Scenario: `live_paired_binary_phase1_tiny.yaml` extends `live_paired_binary_tiny_ws_primary.yaml`
2. Pin `event_end_ts` / `event_start_ts` for BTC 5m window
3. `runtime.strategy_lifecycle.max_runtime_s: null`
4. `survival.enabled: true`; all modules `enforcement_mode: advisory`
5. Run 3–5 markets covering: balanced entry, no-entry, full TP, survivor stall, pre-close flatten
6. Validate: `validate_paired_binary_phase2_live_run.py` + Phase 1 checks
7. After replay confirms advisory matches expectations → enable **selected** `enforce` flags one module at a time

---

## 8. Config changes

**`config/scenarios/live_paired_binary_phase1_tiny.yaml`:**

```yaml
# Inherits live_paired_binary_tiny_ws_primary + survival overlay from phase1.md §11.2
survival:
  enabled: true
  reachability:
    enforcement_mode: advisory
  stall_exit:
    enforcement_mode: advisory
  trailing_stop:
    enforcement_mode: advisory
  economics:
    enforcement_mode: advisory

runtime:
  strategy_lifecycle:
    mode: market_aware
    exit_clock_source: event_end_ts
    max_runtime_s: null
    fallback_max_runtime_s: 900
    flatten_before_event_end_s: 20

paired_binary:
  event_start_ts: <pinned per run>
  event_end_ts: <pinned per run>
```

---

## 9. State / data model changes

None beyond M1–M6 persisted fields.

---

## 10. Facts / observability

This milestone **owns the contract** for all fact payloads; implement centralized builder and schema registration even if individual milestones added constants incrementally.

Add manifest field optional: `phase1_survival_enabled: bool` for validator.

---

## 11. Tests

### `tests/test_paired_binary_phase2_regression.py`

- Run validator classifications against fixture facts from Phase 2 tests
- Assert PHASE2 pass with survival disabled

### `tests/test_paired_binary_survival_monitor.py`

- End-to-end fixture: loser exit → advisory facts emitted → no extra submits vs baseline

### `tests/test_validate_phase1_classifications.py` (optional)

- Synthetic facts → PHASE1_RUNTIME_PREMATURE_EXIT detected

### Replay script smoke test

- live3 facts → stall advisory would fire before max_runtime flatten

---

## 12. Acceptance criteria

- [ ] Phase 2 validation green with `survival.enabled: false`.
- [ ] Phase 1 facts present when survival enabled.
- [ ] `PHASE1_RUNTIME_PREMATURE_EXIT` detects old max_runtime behavior.
- [ ] Replay summary shows advisory decisions without OMS.
- [ ] All survival facts include standard evidence fields.
- [ ] Tiny-live scenario file exists and documents pin protocol.

---

## 13. Risks and rollback plan

| Risk | Mitigation |
|------|------------|
| Fact schema drift | Central builder + validator required keys |
| False PHASE1 pass | Require fact presence + lifecycle completion |

**Rollback:** Disable survival in scenario overlay.

---

## 14. Dependencies and next milestone

**Depends on:** M0–M6 fact emitters (can stub with minimal payloads early).

**Completes:** Phase 1 implementation program.

**After M7:** Operator enables enforce modes per module; tune `flatten_before_event_end_s` with LatencyChain.
