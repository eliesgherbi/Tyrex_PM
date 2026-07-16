# Phase 1 parameter guide

Operational reference for Phase 1 **damage-control** experiments. Scenario profiles live under `config/scenarios/live_paired_binary_phase1_*.yaml`.

Phase 1 is **not** predicting regimes, selecting alpha targets, or optimizing edge. It implements a strict mechanical sequence:

```text
entry → SL hit → survivor hard floor → recovery level → trailing (after recovery) → order policy
```

## 1. Survivor floor — protects the winner immediately

Set at loser exit. Default mode `winner_entry_price`: floor = survivor entry (+ optional buffer).

| Parameter | Controls | Increase → | Decrease → | Facts |
|-----------|----------|------------|------------|-------|
| `survivor_floor.mode` | Floor reference | `winner_entry_price` locks entry | — | `survivor_hard_floor_set` |
| `survivor_floor.floor_buffer` | Offset below reference | Lower floor (earlier exit) | Higher floor (more room) | `survivor_hard_floor_triggered` |
| `survivor_floor.enforcement_mode` | `advisory` vs `enforce` | Facts only | Routes reduce-only exit | enforce facts |
| `survivor_floor.max_spread` | Book quality gate | Stricter | Looser | floor facts |
| `survivor_floor.min_depth_fraction` | Executable depth gate | Avoids thin-book triggers | More triggers | floor facts |
| `survivor_floor.require_fresh_book` | Reject stale books | Fewer false triggers | More triggers | floor facts |

Trigger: executable bid ≤ floor → advisory fact or enforce exit (via Intent → RiskEngine → OMS).

## 2. Recovery level — defines when loss is repaired

Computed at loser exit:

```text
breakeven_price = (total_entry_cash - loser_exit_cash + desired_buffer + slippage_buffer) / survivor_qty
activation_price = breakeven_price + activation_buffer
```

| Parameter | Controls | Facts |
|-----------|----------|-------|
| `recovery_level.desired_buffer` | Extra cash cushion above breakeven | `survivor_recovery_level_computed` |
| `recovery_level.activation_buffer` | Offset above breakeven for trailing arm | same |
| `recovery_level.slippage_buffer` | Slippage cushion (null → strategy default) | same |

No target classification, reachability scoring, or `max_reasonable_exit_price`.

## 3. Trailing — protects gains after recovery

Arms only after recovery when `activation_mode: loss_recovered` (Phase 1 default).

| Parameter | Controls | Increase → | Decrease → | Facts |
|-----------|----------|------------|------------|-------|
| `trailing_stop.activation_mode` | `loss_recovered` / `executable_gain` / `immediate` | — | — | `survivor_trailing_stop_armed` |
| `trailing_stop.recovery_buffer` | Bid must exceed breakeven + buffer | Later arm | Earlier arm | trailing facts |
| `trailing_stop.trail_distance` | Absolute price below peak (NOT %) | Exits earlier | Allows deeper retrace | `survivor_trailing_stop_triggered` |
| `trailing_stop.min_profit_lock` | Minimum trail floor vs entry | Locks more profit | Allows deeper retrace | trailing facts |
| `trailing_stop.arm_delay_s` | Wait after loser exit | Slower arm | Faster arm | trailing facts |
| `trailing_stop.enforcement_mode` | `advisory` vs `enforce` | Facts only | OMS exit on trigger | enforce facts |

Legacy `arm_after_executable_gain` applies only when `activation_mode: executable_gain`.

## 4. Order policy — ensures exits execute

| Parameter | Controls |
|-----------|----------|
| `enforcement.order_policy.mode` | `fak_retry`, `immediate_only`, `fak_then_managed_rest` |
| `enforcement.order_policy.max_fak_retries` | FAK retry count |
| `enforcement.order_policy.reprice_on_retry` | Reprice ticks on retry |

Facts: `survival_enforce_exit_requested`, `survival_exit_order_type_selected`, etc.

## Event-driven monitoring

| Parameter | Controls | Facts |
|-----------|----------|-------|
| `monitor_mode` | `ws_event` (WS only), `hybrid` (WS + poll), `poll` | `survival_monitor_evaluated` |

On each WS book update (or poll heartbeat in hybrid mode), evaluate: hard floor, recovery crossing, trailing updates.

## Runtime / lifecycle

| Parameter | Controls |
|-----------|----------|
| `flatten_before_event_end_s` | Pre-close flatten window (preempts survival enforce) |
| `min_survival_window_s` | Minimum time for survival after activation |
| `fallback_max_runtime_s` | Process safety net |

## Kill switches

| Parameter | Controls |
|-----------|----------|
| `per_pair_max_loss_usd` | Per-pair stop |
| `daily_max_loss_usd` | Session stop |

Facts: `kill_switch_triggered`.

## Removed from Phase 1 flow

- `target_policy` / `max_reasonable_exit_price`
- `stall_exit` timers and progress tracking
- Reachability/economics enforce paths (advisory config may remain but is not orchestrated)
