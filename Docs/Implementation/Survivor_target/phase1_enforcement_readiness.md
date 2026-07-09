# Phase 1 enforcement readiness

## Behavior-changing today

| Module | Always active when `survival.enabled` | Enforce wired |
|--------|----------------------------------------|---------------|
| **Target policy (M1)** | Yes — reprices survivor after loser exit | N/A (always applied) |
| **Exit planner (M2)** | Yes — executable bid evidence | Used by enforce dispatch |
| **Kill switches (M6)** | When enabled | Yes — shutdown force flatten |
| **Max-runtime survivor flatten** | Runtime config | Yes — shutdown path |

## Advisory-only (facts, no survival OMS)

| Module | Facts | `enforce_exit` signal |
|--------|-------|----------------------|
| Reachability | `survivor_reachability_scored` | Never (observability only) |
| Stall (default `downgrade`) | `survivor_stall_detected` | Downgrade only when enforce + stalled |
| Trailing stop (advisory) | `survivor_trailing_stop_armed/triggered` | Cleared in advisory mode |
| Economics (advisory) | `survivor_economics_evaluated` | Verdict downgraded to PROCEED |

## Enforce wiring (after this patch)

| Module | Trigger type | OMS path |
|--------|--------------|----------|
| Trailing stop | `survival_trailing_stop` | Intent → Risk → Planner → OMS |
| Stall `exit_*` | `survival_stall_exit` | Same |
| Stall `downgrade` | — | Target downgrade via `SurvivorTargetPolicy` |
| Economics | `survival_economics_exit` | Same (disabled in live profiles) |

Facts: `survival_enforce_exit_requested`, `survival_enforce_exit_submitted`, `survival_enforce_exit_skipped`.

## Safe first enforcement candidates

1. **Dynamic target policy** — already live; test with `live_paired_binary_phase1_target_only`
2. **Trailing enforce** — one reduce-only exit on trail floor breach
3. **Stall enforce (downgrade)** — lowers target without OMS

**Not first candidate:** economics enforce, reachability enforce, stall `exit_market` until more evidence.

## Parameters → facts map

| Parameter area | Key facts |
|----------------|-----------|
| Target policy | `survivor_target_selected`, `survivor_target_downgraded` |
| Trailing | `survivor_trailing_stop_armed`, `survivor_trailing_stop_triggered`, enforce facts |
| Stall | `survivor_stall_detected`, `survivor_progress_evaluated`, enforce/downgrade facts |
| Reachability | `survivor_reachability_scored` |
| Economics | `survivor_economics_evaluated`, `survivor_early_exit_triggered` |

## Scenario profiles

- `live_paired_binary_phase1_advisory` — all advisory
- `live_paired_binary_phase1_target_only` — dynamic target only
- `live_paired_binary_phase1_trailing_enforce` — trailing enforce only
- `live_paired_binary_phase1_stall_enforce` — stall enforce only (default downgrade)

Preflight rejects >1 enforce module and economics enforce.
