# `survival/`

Phase 1 **damage-control** layer for paired-binary survivor management. Evaluates executable exit quality, advisory/enforce modules, and OMS dispatch policy **after** the loser leg stops out.

**Default:** `runtime.survival.enabled: false` — no behavior change until explicitly enabled in a scenario overlay.

## Role in the stack

```text
PairedBinaryMonitor.tick (survivor phase)
  → evaluate_survival_advisory (advisory.py)
       → SurvivalExitPlanner (exit_planning.py) + DataQualityGate
       → SurvivorHardFloor / SurvivorTrailingStop / …
  → enforcement_dispatch (when enforce_mode = enforce)
       → select_survival_exit_order (order_policy.py)
       → IntentWorkUnit → pipeline → RiskEngine → OMS
```

Survival modules **never** submit to the venue directly. They emit facts and delegate exits through the same `Intent → RiskEngine → ExecutionPlanner → OMS` spine as the core strategy.

## Phase 1 simplified flow (current default profile)

When `stall_exit.enabled: false` and simplified advisory is active:

```text
entry → pair stop (loser) → hard floor (advisory) → recovery level → trailing (enforce) → order policy
```

Legacy modules (`target_policy`, `reachability`, `stall_exit`, `economics`) remain in the package for earlier milestones but are **not** on the default Phase 1 live path.

## Files

| File | Purpose |
|------|---------|
| `advisory.py` | Orchestrates floor + trailing evaluation; sets `enforce_exit` when module `enforcement_mode: enforce` |
| `survivor_floor.py` | Hard floor price + breach detection (`advisory` vs `enforce`) |
| `recovery_level.py` | Breakeven / activation price after loser exit |
| `trailing_stop.py` | Arm after recovery; trigger on trail-floor breach |
| `exit_planning.py` | Depth-aware exit eval via `ExecutableBookView` + quality gate |
| `enforcement_dispatch.py` | Enforce payload, pending intent latch, FAK-reject hooks, pre-close preempt |
| `order_policy.py` | FAK retry / managed-rest selection for survival exits |
| `quality_reject_detail.py` | Maps gate report reasons → freshness/spread/depth/sequence_gap |
| `enforcement.py` | `is_enforce()` helper (`advisory` \| `enforce`) |
| `models.py` | Domain types (`SurvivalAdvisoryResult`, `ExecutableExitEvidence`, …) |
| `facts.py` | Internal fact payload builders |
| `kill_switches.py` / `kill_switch_runtime.py` | Optional survival kill switches (default off) |
| `stall_exit.py`, `reachability.py`, `target_policy.py`, `economics.py` | Legacy Phase 1 milestones (not default simplified flow) |

## Enforcement modes

| Mode | Behavior |
|------|----------|
| `advisory` | Compute, store, emit facts; **no** survival OMS submit on trigger |
| `enforce` | Same evaluation; on trigger → `survival_enforce_exit_*` → reduce-only exit via monitor |

Per-module: `survivor_floor`, `trailing_stop`, `economics`, `stall_exit`, `reachability` each have their own `enforcement_mode`. Global defaults remain **`advisory`** except where a scenario explicitly sets e.g. `trailing_stop.enforcement_mode: enforce`.

## Quality reject retry (pre-submit)

When enforce dispatch skips with `skip_reason=quality_reject` and `enforcement.retry_quality_rejects: true`:

1. Latch `pending_survival_exit_intent` on survivor leg state
2. On subsequent WS monitor ticks, re-evaluate with `DecisionContext.URGENT_EXIT` after backoff
3. Submit when quality passes; abandon on max attempts/time or pre-close flatten

See [quality_reject_retry_root_cause.md](../../Implementation/Survivor_target/quality_reject_retry_root_cause.md).

## Post-submit retry (OMS FAK reject)

Separate path: `handle_survival_exit_oms_reject` → `TP_PENDING_*` phase → `_retry_pending_exits` with repricing via `order_policy`.

## Configuration

Under `runtime.survival` in scenario YAML. Authoritative field reference: [CONFIG_MODEL.md §4.1](../../CONFIG_MODEL.md#41-survival-phase-1) and [phase1_parameter_guide.md](../../Implementation/Survivor_target/phase1_parameter_guide.md).

## Facts

Declared in `reporting/schema_v2.py` (`PHASE1_SURVIVAL_FACT_TYPES`). Key types:

- `survivor_hard_floor_set`, `survivor_trailing_stop_armed`, `survivor_trailing_stop_triggered`
- `survival_enforce_exit_requested`, `survival_enforce_exit_submitted`, `survival_enforce_exit_skipped`
- `survival_enforce_exit_retry_scheduled`, `survival_enforce_exit_retry_attempted`, `survival_enforce_exit_abandoned`

Validate live runs: `scripts/validate_paired_binary_phase2_live_run.py`.

## Boundaries

- Must not import `venue/*` or call OMS directly
- May read `MarketStateStore` via coordinator + `SurvivalExitPlanner`
- Must not mutate `AllocationLedger` / `WalletStore`
