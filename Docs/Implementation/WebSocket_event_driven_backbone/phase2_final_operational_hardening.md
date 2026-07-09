# Phase 2 final operational hardening

**Date:** 2026-06-29  
**Status:** implemented (fixture-validated; selective live gaps documented)

---

## Problem

Live batch `live3` / `live4` recovered persisted `DONE` from `live2` despite different token IDs, exiting immediately with **0 ticks**. Terminal lifecycle state must not block a new market run.

Secondary: `live1` ended without `paired_binary_loop_stopped` (likely manual process kill; loop now emits health facts from `finally` on all in-process exits).

Open-exposure force-flatten was implemented but not live-proven before this pass.

---

## Fix 1 — market-aware state recovery

**Module:** `src/tyrex_pm/runtime/paired_binary_recovery.py`

| Condition | Behavior |
|-----------|----------|
| No persisted file | Fresh `IDLE` + wallet/allocation bootstrap (unchanged) |
| Token pair mismatch | Ignore lifecycle; emit `paired_binary_state_recovery_ignored`; reset `IDLE`; wallet bootstrap independent |
| Same pair + terminal (`DONE`/`FAILED`/`PAIR_ABORTED_FLAT` saga) + `allow_terminal_state_resume=false` (default) | Emit `paired_binary_terminal_state_reset`; reset `IDLE` |
| Same pair + terminal + `allow_terminal_state_resume=true` | Preserve terminal (legacy resume) |
| Same pair + non-terminal open exposure | Recover lifecycle; emit `paired_binary_state_recovery_applied` (`valid_open_exposure_recovery`) |

**Config (default false):**

```yaml
runtime:
  paired_binary:
    allow_terminal_state_resume: false
```

**Facts:** `paired_binary_state_recovery_checked`, `_applied`, `_ignored`, `paired_binary_terminal_state_reset`

---

## Fix 2 — loop exit health facts

**Module:** `src/tyrex_pm/runtime/paired_binary_run.py`

| Exit path | Health `event` |
|-----------|----------------|
| Normal break / terminal | `paired_binary_loop_stopped` |
| Uncaught exception | `paired_binary_loop_failed` (+ `error`) |
| `asyncio.CancelledError` / `KeyboardInterrupt` | `paired_binary_loop_interrupted` |

Persistence flush moved to `finally` so abrupt in-process exits still persist and emit.

**Note:** External `SIGKILL` / task manager kill cannot emit facts.

---

## Fix 3 — open-exposure force-flatten validation

**Proof:** `tests/test_paired_binary_open_exposure_force_flatten_validation.py` (Option A fixture)

Validates:

- `BOTH_LEGS_ACTIVE` at `max_runtime_s`
- `paired_binary_open_exposure_at_shutdown`
- Reduce-only SELL intents for both legs
- No REST-sourced OMS submits
- `paired_binary_loop_stopped` on exit

**Live proof:** live3 (`paired_binary_ws_primary_live3_1782903844`) — survivor YES force-flattened at max_runtime; legacy facts lack `paired_binary_done` (pre-fix). Re-run required for `PHASE2_WS_OPEN_EXPOSURE_FORCE_FLATTEN_PASS`.

---

## Fix 4 — shutdown DONE reporting (2026-06-29)

After successful force-flatten to flat inventory, emit:

- `paired_binary_shutdown_force_flatten_done` (existing)
- `paired_binary_done` with `completion_reason=shutdown_force_flatten`
- `paired_binary_realized_pnl` or `paired_binary_realized_pnl_unavailable` with structured `reason`

Phase 2 validator (`scripts/validate_paired_binary_phase2_live_run.py`) classifies:

- `PHASE2_WS_LIFECYCLE_PASS` — normal stop/TP DONE
- `PHASE2_WS_OPEN_EXPOSURE_FORCE_FLATTEN_PASS` — max_runtime flatten DONE
- `PHASE2_WS_FAIL` — incomplete flatten or missing reporting facts

---

## Scenario YAML fix

`config/scenarios/live_paired_binary_tiny_ws_primary.yaml`: `paired_binary` runtime block was incorrectly nested under `observability`; moved to `runtime.paired_binary`.

---

## Tests

| File | Cases |
|------|-------|
| `tests/test_paired_binary_state_recovery_identity.py` | DONE→IDLE, BOTH_LEGS_ACTIVE recover, resume flag, post-DONE ticks |
| `tests/test_paired_binary_terminal_state_reset.py` | FAILED new tokens, DONE reset, resume preserves DONE |
| `tests/test_paired_binary_state_recovery_token_mismatch.py` | DONE mismatch, BOTH_LEGS_ACTIVE mismatch + wallet bootstrap |
| `tests/test_paired_binary_loop_health_facts.py` | stopped / failed |
| `tests/test_paired_binary_open_exposure_force_flatten_validation.py` | force-flatten integration |
| `tests/test_paired_binary_shutdown_done_reporting.py` | shutdown emits paired_binary_done |
| `tests/test_paired_binary_shutdown_pnl_reporting.py` | PnL or unavailable on shutdown DONE |
| `tests/test_validate_phase2_force_flatten_classification.py` | Phase 2 validator classifications |

---

## Remaining risks before Phase 1 / Phase 3

1. Live re-run after DONE on **same** token pair still needs explicit `allow_terminal_state_resume: true` to no-op (by design).
2. Legacy live3 facts predate shutdown DONE reporting — re-run for full `PHASE2_WS_OPEN_EXPOSURE_FORCE_FLATTEN_PASS`.
3. `live1`-class abrupt external kills will still lack loop health facts.
4. Persisted state path is `{state_dir}/paired_binary/{owner_id}/{market_id}.json` — same `market_id` with different tokens is now handled; operators should still prefer distinct `market_id` per market when possible.

---

## Out of scope (unchanged)

Entry/TP/SL thresholds, survivor repricing, position sizing, market selection, Phase 1 trailing stops, Phase 3 signals.
