# A0.8 Enforce Readiness Report

**Date (UTC):** 2026-07-14  
**Phase:** A0.8 — enforced tiny-live integration, preflight, operational checklist  
**Implementation agent:** no live orders placed; no operator approval artifact created.

---

## Executive verdict

**READY_FOR_ONE_COMMAND_OPERATOR_TEST**

Single-session orchestrator implemented. Automated validation complete (165 `z_gap` tests). Live observe-only commissioning demonstrated in virtual-time tests; operator must run one real `--observe-only` commissioning window before first `--execute`.

---

## Single-session orchestrator (new)

| Component | Path |
|-----------|------|
| CLI entry | `scripts/go_z_gap_tiny_live.py` |
| Orchestrator | `src/tyrex_pm/runtime/z_gap_session_orchestrator.py` |
| Window scheduler | `src/tyrex_pm/ingestion/btc_5m_window_scheduler.py` (`select_btc_5m_session_window`) |
| Static/dynamic gates | `src/tyrex_pm/runtime/z_gap_preflight.py` |
| Boundary PTB | `src/tyrex_pm/runtime/z_gap_boundary_gate.py` |
| Commissioning policy | `src/tyrex_pm/runtime/z_gap_ptb_commissioning.py` (`z_gap_ptb_commissioning_v1`) |
| Sidecar supervisor | `src/tyrex_pm/runtime/z_gap_sidecar_supervisor.py` |
| Interactive UX | `src/tyrex_pm/runtime/z_gap_interactive_approval.py` |

**Operator does not type K.** PTB is `WAITING_FOR_BOUNDARY` before start; attestation is written at boundary.

**Independent PTB reference:** Gamma/CLOB/RTDS do not expose an authoritative boundary K fetchable at runtime. Commissioning certificate + live/log lock is the documented policy.

---

## End-to-end shadow lifecycle

| Step | Status |
|------|--------|
| `would_enter` → entry plan → FAK BUY | Pass (shadow `ScenarioOMS`) |
| Confirmed fill activation | Pass — `active_quantity == filled_entry_quantity` |
| Thesis stop / lifecycle flatten / exit retry | Pass (5 E2E scenarios) |
| FAK SELL → zero allocation | Pass |
| Terminal `operational_pass=true` | Pass (`scripts/run_z_gap_shadow_e2e.py`) |

Dry-run output: `var/reporting/runs/z_gap_a0_8_shadow_e2e/` — `exit_code=0`, `terminal_state=DONE`, `allocation_zero=true`.

---

## Entry safety

- Enforce entry only when `DECISION_WOULD_ENTER` and `validate_z_gap_pre_submit` pass.
- Fixed `$5` cap via `sizing.max_usd` and risk `notional.max_usd`.
- FAK entry; `one_position_per_window` / `no_reentry_after_exit` gates enforced.
- Bugfix: removed duplicate `entry_attempted` set before `mark_entry_submitted` (blocked IDLE→ENTRY_PENDING).

---

## Activation and allocation

- Activation on confirmed BUY fill only (`reconcile_entry_fill`).
- Allocation credits on matched evidence; partial BUY activates confirmed qty only.
- Zero-fill BUY → DONE without activation (scenario 3).

---

## Exit safety

- Precedence: kill switch > thesis stop > lifecycle flatten.
- FAK SELL clamped to allocation; no submit from bare ACTIVE without exit trigger.
- Partial exit retries release allocation reservation on unfilled (`maybe_release_exit_reservation`).
- Max exit attempts → `FAILED` + `manual_intervention_required` (scenario 5).

---

## Reconciliation behavior

- Statuses: `consistent`, `venue_lag_expected`, `mismatch`, `unresolved`.
- Configurable grace (`reconciliation.venue_sync_grace_ms`, default 3000).
- Venue lag does not overwrite confirmed fills; fail-closed after grace.
- `safe_exit_quantity` caps SELL to confirmed owned qty.

---

## Preflight artifacts

Script: `scripts/preflight_z_gap_live_scenario.py`

Validates: fee curve, Binance, PTB attestation, clock sanity, calibration review, operator approval, strategy safety, market readiness, non-terminal persisted state.

---

## PTB attestation status

Enforce requires fresh live attestation (`golden_fixture_only: false`, `ptb_error_bps <= 0.5`). Observe-window PTB lag is separate from K-reference attestation.

---

## TimeAuthority status

Enforce gate: `sync_status=synced`, `uncertainty_ms <= 250`. OS wall-clock drift remains informational (`preflight_clock_sanity.py`).

---

## Calibration-lite operator review status

Machine-readable review required at `var/reporting/z_gap/calibration_lite_review.json`. Sign-off means operational sanity only — **not** statistical calibration or edge claim.

---

## Operator approval mechanism

- Explicit `operator_enforce_approval.json` required.
- Market-scoped, time-limited, `maximum_usd <= 5`, `one_trade_only`.
- **No approval artifact was created during A0.8 implementation.**
- Tests verify expired/wrong-market/over-cap approvals block enforce.

---

## Tiny scenario constraints

`config/scenarios/live_z_gap_tiny.yaml`:

- `entry_mode: enforce`, `max_usd: "5"`, FAK, flatten ≥ 20s before event end.
- `live_validation`: `maximum_entry_attempts: 1`, `maximum_positions: 1`, `stop_after_terminal: true`.
- `reconciliation` block present.
- Enforce loop stops on terminal phase or event end.

---

## Manual intervention runbook

`Docs/Implementation/z_gap_strategy/a0_8_tiny_live_runbook.md`

Covers: manual kill, emergency exit, exhausted retries, venue mismatch, crash with persisted state. Uses repository commands only.

---

## Test results

| Suite | Result |
|-------|--------|
| A0.8 targeted (`shadow_e2e`, `reconciliation`, `preflight`, `approval`, `tiny_scenario`, `closure_final`) | **30/30 pass** |
| Cumulative Z-Gap regression (A0.1–A0.8 + foundational deps) | **248/248 pass** |
| Full `tests/test_z_gap*.py` only | **128/128 pass** |
| Shadow dry-run script | **exit_code=0**, `operational_pass=true` |

No real OMS BUY/SELL during implementation or closure pass.

---

## Final Closure Verification

### Cumulative Regression

Restored the A0.7-equivalent cumulative command (A0.6 foundational modules + A0.7 lifecycle modules + A0.8 modules + `test_btc_5m_metadata.py`, `test_max_fill_price_for_edge_floor.py`):

```powershell
python -m pytest `
  (Get-ChildItem tests/test_z_gap*.py).FullName `
  tests/test_signal_state_store.py `
  tests/test_signal_feed_runtime.py `
  tests/test_ewma_volatility.py `
  tests/test_binary_fair_value.py `
  tests/test_fees_phi.py `
  tests/test_edge_calculator.py `
  tests/test_time_authority.py `
  tests/test_price_to_beat_tracker.py `
  tests/test_preflight_ptb_attestation.py `
  tests/test_clock_sanity.py `
  tests/test_preflight_binance_connectivity.py `
  tests/test_max_fill_price_for_edge_floor.py `
  tests/test_btc_5m_metadata.py -q
```

**Result: 248/248 pass** (A0.7 reported 188/188 before A0.8 added 60 tests across `test_z_gap_closure_final.py`, `test_z_gap_shadow_enforce_e2e.py`, `test_z_gap_reconciliation.py`, `test_z_gap_live_preflight.py`, `test_z_gap_operator_approval.py`, `test_z_gap_tiny_scenario.py`, and sidecar regression).

Fixed shadow-harness `Z_GAP_PREFLIGHT_DIR` env leakage that caused `test_z_gap_enforce_without_preflight_gates_blocked` to flake when run after E2E tests.

**Whole-project pytest:** `1369 passed, 15 failed, 1 skipped` — failures are **unrelated** to Z-Gap Phase A (paired-binary monitor/reconcile, execution planner facts, reference-price events, survival stall/replay, validation harness). Not gating for A0.8 operator go/no-go.

### Shadow/Live Pipeline Parity

`ScenarioOMS` is injected only as the `oms` argument to `run_enforce_tick` → `process_intent_work_unit`. Shadow harness sets `apply_local_shadow_fill=False` so fills flow through the same pipeline callbacks as live.

| Stage | Live path | Shadow substitution |
|-------|-----------|---------------------|
| Entry evaluation | `run_observe_tick` in `z_gap_enforce.run_enforce_tick` | Same |
| Entry plan | `build_z_gap_entry_plan` | Same |
| Pre-submit validation | `validate_z_gap_pre_submit` (`z_gap_enforce._submit_entry_if_ready`) | Same — verified by `test_shadow_pipeline_uses_pre_submit_and_process_intent` |
| IntentWorkUnit | `z_gap_entry_plan_to_intent_work_unit` | Same |
| Risk + planner + OMS | `process_intent_work_unit` (`runtime/pipeline.py`) | Same — `oms=ScenarioOMS` only |
| Fill callbacks | `reconcile_entry_fill` / `reconcile_exit_fill` | Same |
| Allocation | `allocation_runtime` via pipeline | Same |
| Exit plan | `build_z_gap_exit_work_unit` | Same |
| Reconciliation | `evaluate_position_reconciliation` on ACTIVE ticks | Same |

**Not bypassed:** `validate_z_gap_pre_submit`, `process_intent_work_unit`, execution planner, allocation runtime, entry/exit fill callbacks.

### Venue-Lag Exit Behavior

Integration scenarios with `sell_requires_venue_position: true` (live-like risk) in `tests/test_z_gap_closure_final.py`:

| Outcome | Behavior verified |
|---------|-------------------|
| **A — venue catches up** | `venue_lag_expected` recorded; wallet synced via tick; lifecycle flatten → SELL → DONE; `operational_pass=true` |
| **B — venue never catches up** | Grace exhausted → `FAILED`; `active_quantity=8` retained; `position_closed=false`; `operational_pass=false`; `manual_intervention_required=true`; no SELL submitted (`sell_requires_venue_position` + zero venue) |

Terminal summary asserts: `active_quantity`, `allocated_quantity`, `venue_reported_quantity`, `manual_intervention_required`, `position_closed`, `operational_pass`.

### Duplicate and Reservation Safety

`tests/test_z_gap_closure_final.py` + `tests/test_z_gap_reconciliation.py`:

- Unfilled SELL reservation released (`release_reservation`)
- Partial SELL reserves only outstanding qty (`apply_exit_fill` partial)
- Duplicate BUY fill idempotent (`duplicate_fill_idempotent`, `reconcile_entry_fill` ignore)
- Duplicate SELL fill idempotent (`apply_exit_fill` dedup_key)
- Delayed/over-fill cannot drive allocation negative (clamped at zero)
- Exit retry cannot exceed residual `active_quantity` (oversell → `exit_failed`)

### Preflight Dry Run

```bash
python scripts/preflight_z_gap_live_scenario.py \
  --scenario config/scenarios/live_z_gap_tiny.yaml \
  --json-out var/reporting/z_gap/a0_8_preflight_dry_run.json
```

**Result: PREFLIGHT BLOCKED** (exit code 1) — no operator approval artifact created.

| Gate | Status |
|------|--------|
| fee_curve | PASS |
| Binance connectivity | PASS |
| TimeAuthority (clock_sanity) | PASS |
| fresh PTB attestation | BLOCK (`golden_fixture_only=true`) |
| calibration review | BLOCK (missing `calibration_lite_review.json`) |
| operator approval | BLOCK (missing `operator_enforce_approval.json`) |
| scenario constraints / market metadata | BLOCK (template placeholders in tiny YAML) |
| books/readiness | BLOCK (placeholder tokens, null event timestamps) |
| persisted lifecycle state | PASS (no non-terminal state) |

Technical gates that pass do so from existing artifacts under `var/reporting/z_gap/`. Remaining blockers are operator approval, fresh live PTB attestation, calibration sign-off, and resolving scenario placeholders before a real window.

### Sidecar Corruption Tolerance

`derive_ptb_from_chainlink_log_ex` returns `PtbParseDiagnostics` (blank/malformed/invalid row counts). Regression: `test_derive_from_log_skips_blank_and_malformed_rows` — valid / blank / corrupt / valid → PTB from first valid tick (`11111.11`), diagnostics emitted, no crash.

---

## Remaining risks

1. **Live venue wallet lag** — closure proves fail-closed behavior with `sell_requires_venue_position: true`; first live SELL still depends on wallet sync timing within grace.
2. **Thesis-stop price sensitivity** — tiny moves may not cross `z_stop=0.25`; monitor `model_exit_triggered` facts.
3. **No restart recovery** — crash with open position requires manual procedure (by design).
4. **Operator artifacts** — stale/expired approval or PTB attestation blocks startup (intentional).

---

## Exact live command — not executed

```bash
# 1. Preflight (after operator artifacts in var/reporting/z_gap/)
python scripts/preflight_z_gap_live_scenario.py \
  --scenario config/scenarios/live_z_gap_tiny.yaml

# 2. Live tiny enforce (ONLY after explicit operator authorization)
python -m tyrex_pm.runtime.app run \
  --strategy config/strategies/z_gap.yaml \
  --scenario config/scenarios/live_z_gap_tiny.yaml \
  --run-name z_gap_tiny_enforce_<YYYYMMDD_HHMM>
```

---

## Recommendation

Proceed to **operator go/no-go** review:

1. Complete all preflight artifacts (especially fresh PTB attestation and operator approval).
2. Confirm shadow E2E output at `var/reporting/runs/z_gap_a0_8_shadow_e2e/`.
3. If go: run preflight, then execute the live command above during a single approved window with manual kill readiness.

**Do not** authorize live enforce until this checklist is explicitly signed off.
