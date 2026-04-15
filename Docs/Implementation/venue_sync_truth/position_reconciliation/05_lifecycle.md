# Position Reconciliation — Lifecycle

## When the reconciliation pass runs

The reconciliation pass runs as part of the existing `_sync_cycle` timer, **not** on a
separate timer. The cycle sequence is:

1. **HTTP fetch** — positions and orders from Data API / CLOB (existing).
2. **Discovery pass** — resolve missing instruments into Cache (existing).
3. **Reconciliation pass** — diff venue positions vs cache positions, collect actions (new).
4. **Result emission** — fact emission and action application on event-loop thread.

Steps 1–3 run in the executor thread via `run_in_executor`. Step 4 runs on the event-loop
thread, scheduled via `self._event_loop.call_soon_threadsafe(...)` from the executor
thread at the end of step 3. Actions are applied on the next event-loop iteration — no
queue, no extra poll-cycle latency.

### Justification for same timer

A separate timer would introduce complexity:

- Two timers can fire concurrently, creating races between discovery and reconciliation.
- The reconciliation pass needs the positions payload from step 1 — fetching it twice
  wastes API quota and adds latency.
- The cycle-in-progress guard (Race F defense) would need to coordinate across two timers.

Using the same timer ensures discovery always precedes reconciliation, and the positions
payload is fetched once per cycle.

## Cycle timing

With `poll_interval_seconds=15`:

| Phase | Typical duration | Notes |
|-------|-----------------|-------|
| HTTP fetch (positions + orders) | 2–5s | Data API + CLOB, sequential |
| Discovery pass | 0–2s | Only when new instruments found |
| Reconciliation pass (diff + defense checks) | <100ms | In-memory only |
| Action application (msgbus.send calls) | <10ms per report | Synchronous engine handler |
| **Total cycle** | **3–8s** | Well within 15s interval |

## Startup behavior

Position reconciliation does **not** run until `first_sync_complete` is `True`. This
ensures that:

1. The instrument cache is populated before position diffs are computed.
2. Startup reconciliation (engine's own `reconcile_execution_state`) has completed
   (gated by `StartupReadinessGate`'s existing readiness checks).
3. The engine's `_startup_reconciliation_event` is set, meaning
   `_continuous_reconciliation_loop` is running and `reconcile_execution_report`
   endpoint is actively processing.

If `first_sync_complete` is `False`, `_reconciliation_pass` returns an empty action list.

## Interaction with the engine's own position checks

The engine's `_check_positions_consistency` runs on its own timer
(`position_check_interval_secs`, default configurable). It calls the adapter's
`generate_position_status_reports` and reconciles via the same
`_reconcile_position_report_netting` path.

These two reconciliation sources may overlap:

- **Engine position check** runs on its own schedule, using the adapter's report generators.
- **WalletSyncActor reconciliation** runs on the wallet-sync poll timer, using the Data API
  positions payload.

Both feed into the same `_reconcile_position_report_netting` method, which is idempotent:
if one has already reconciled a discrepancy, the other sees `quantities_match == True`
and takes no action.

The engine's position check may also trigger `_query_and_find_missing_fills`, which
queries the adapter for fill reports. This is complementary: the actor provides the fast
path (Data API → `PositionStatusReport` → synthetic reconciliation), while the engine's
fill-based reconciliation provides the accurate path (real trade data → exact prices).

## Health source interaction

The health source (`NautilusLiveExecutionHealthSource`) evaluates wallet-sync health in
a priority chain. The new `stuck_deferral_count` property is checked after the existing
stale/unresolvable rules:

```
Rule 1: Engine reconciliation not done → UNKNOWN_BOOTSTRAP
Rule 2: Wallet sync pending, deadline not exceeded → UNKNOWN_BOOTSTRAP
Rule 3: Wallet sync pending, deadline exceeded → DEGRADED_OMS
Rule 4: Wallet sync stale → DEGRADED_OMS (wallet_sync_stale)
Rule 5: Wallet sync unresolvable instruments → DEGRADED_OMS
Rule 6: Both stale and unresolvable → DEGRADED_OMS (stale wins)
Rule 6.5 (NEW): Stuck reconciliation deferrals → DEGRADED_OMS
        (position_reconciliation_stuck)
Rule 7: HEALTHY
```

**Rationale for priority:** Stale wallet sync (rules 4–6) implies the sync cycle itself
is failing, which subsumes deferral concerns. Stuck deferrals only matter when the sync
cycle is running normally but in-flight orders keep preventing reconciliation past the
max deferral count.

### Health rule detail

- **Reason code:** `position_reconciliation_stuck`
- **When triggered:** `ws.stuck_deferral_count > 0` — at least one instrument has been
  deferred for `position_reconciliation_deferral_max` consecutive cycles.
- **Effect on risk:** `DEGRADED_OMS` with the `position_reconciliation_stuck` reason code.
  `ConfiguredRiskPolicy` treats `DEGRADED_OMS` as blocking for BUY orders (existing
  behavior). Operators should investigate the in-flight order that's preventing
  reconciliation.
- **Recovery:** When the in-flight order fills or is cancelled, the next cycle clears the
  deferral (venue qty now matches cache qty, or the discrepancy changes direction).

## Shadow mode

When `position_reconciliation_enabled=true` and `position_reconciliation_shadow_mode=true`,
the reconciliation pass runs normally: HTTP fetch, discovery, diff algorithm, race
defenses. `ReconciliationAction` objects are computed and dispatched to
`_apply_reconciliation_actions` via `call_soon_threadsafe`. However,
`_apply_reconciliation_actions` skips the `msgbus.send` call and instead emits the
`position_reconciliation` fact with `reconciliation_sent=false`.

**Operational rollout:**

1. Deploy with `position_reconciliation_enabled=true`, `position_reconciliation_shadow_mode=true`.
2. Run for a defined validation period (recommended: one week of production traffic).
3. Inspect emitted `position_reconciliation` facts in the reporting artifacts. Verify:
   - Diff directions match actual wallet activity.
   - No false positives (diffs that should be no-ops).
   - Deferrals resolve correctly (in-flight orders fill, then diff disappears).
   - Race B defense (ts_last debounce) isn't deferring indefinitely.
4. When confident, set `position_reconciliation_shadow_mode=false`. The actor begins
   injecting `PositionStatusReport` objects into the engine.

**Temporary rollout tool:** Shadow mode is expected to be removed once the actor's
diff behavior is validated in production. A follow-up cleanup task should remove the
config key and the branching logic in `_apply_reconciliation_actions`.

## Shutdown behavior

On `on_stop`:

1. `clock.cancel_timer("wallet_sync")` (existing) — prevents new cycles.
2. `cancel_all_tasks()` (existing) — cancels any running executor task.
3. No reconciliation actions pending in memory survive stop — `_deferred_reconciliations`
   is ephemeral and not persisted.

There is no attempt to run a "final reconciliation" on shutdown: the positions will be
re-reconciled on the next startup's reconciliation pass.

## Observability

### New fact type: `position_reconciliation`

Emitted once per instrument per cycle when a position discrepancy is detected:

```json
{
  "fact_type": "position_reconciliation",
  "cycle": 42,
  "instrument_id": "YES-1234..POLYMARKET",
  "venue_qty": "0.0",
  "cache_qty": "50.0",
  "diff_direction": "close",
  "deferred": false,
  "defer_count": 0,
  "reconciliation_sent": true
}
```

Possible `diff_direction` values:

| Value | Meaning |
|-------|---------|
| `close` | Venue quantity is 0, cache has open position. Full close. |
| `partial_reduce` | Venue quantity > 0 but < cache quantity. Partial reduction. |
| `deferred` | Discrepancy detected but deferred due to in-flight orders. |
| `skipped_ttl` | Discrepancy detected but skipped due to recently-reconciled TTL. |
| `venue_has_more` | Venue > cache. Only emitted when `reconcile_venue_has_more=True`. |

### Log messages

**Normal reconciliation:**
```
INFO  event=position_reconciliation_sent component=wallet_sync instrument_id=YES-1234..POLYMARKET venue_qty=0.0 cache_qty=50.0 direction=close
```

**Deferred reconciliation:**
```
INFO  event=position_reconciliation_deferred component=wallet_sync instrument_id=YES-1234..POLYMARKET venue_qty=0.0 cache_qty=50.0 defer_count=2 inflight_sell_qty=50.0
```

**Stuck deferral (max reached):**
```
WARNING  event=position_reconciliation_stuck component=wallet_sync instrument_id=YES-1234..POLYMARKET defer_count=5 proceeding=true
```

**Recently-reconciled skip:**
```
DEBUG  event=position_reconciliation_skipped_ttl component=wallet_sync instrument_id=YES-1234..POLYMARKET ttl_remaining_s=45.2
```
