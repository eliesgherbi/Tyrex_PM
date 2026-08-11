# Phase 10 operator monitoring fields

Read-only snapshot produced by `tyrex_pm.runtime.n7_operator_monitor.build_operator_monitor_snapshot`
and exposed on `N7OneShotHost.operator_monitor_snapshot()` / `status()["operator_monitor"]`.

Reporting may emit the same payload via `emit_operator_monitor_snapshot` (evidence only; not authority).

| Field | Meaning |
|---|---|
| `lifecycle_phase` | Current `MutationPhase` (or `MUTATIONS_DISABLED` / `ARMED` fallback) |
| `obligations` | Obligation registry summary (open/blocking/unknown + rows) |
| `matched_qty` | Strategy-owned matched exposure (wakes protection; not sellable) |
| `confirmed_qty` | Confirmed settlement inventory |
| `sellable_qty` | Authenticated SELL cap |
| `remaining_qty` | Exit remaining / unmatched residual projection |
| `open_owned_orders` | Non-terminal owned orders with remaining quantity |
| `stream_health` | User-stream `ready` / `running` / `gap` / `stopped` / `healthy` |
| `exit_supervisor` | Supervisor state + current decision (deadline visible) |
| `reconciliation` | Last baseline-aware recon result (if any) |
| `reporting_health` | Reporter attached + critical-lane health signal |
| `real_venue_mutation_count` | Count of real venue mutations this session |
| `mutations_force_off` | Terminal / handoff force-off latch |
| `mutations_ready` | Whether new mutations could arm/submit |
| `operator_live_armed` | Whether host completed real operator arming |
| `authorization_source` | `cli_--live` when armed; otherwise null |
| `sealed_fingerprint` | Automatic sealed config fingerprint (reports/recovery) |
| `terminated` | Host terminated flag |
| `captured_at` | UTC timestamp of snapshot |

## Stop → handoff

On any Phase 10 stop condition, call `N7OneShotHost.enter_manual_handoff(...)`.
That **disables mutations first**, then returns a handoff report with:

- known owned quantity
- open order IDs
- venue evidence (obligations / recon / stream)
- `next_safe_operator_action`
- explicit `do_not`: guess inventory, unbounded emergency SELL, re-arm without authorization
