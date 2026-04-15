# Position Reconciliation — Migration Steps

## Step 1: Config surface and `WalletSyncConfig` extension

**Depends on:** Nothing.

**Scope:**

1. Add the 6 new config keys to `WalletSyncConfig` in `runtime/wallet_sync.py`:
   - `position_reconciliation_enabled: bool = False`
   - `position_reconciliation_shadow_mode: bool = True`
   - `data_api_lag_tolerance_seconds: float = 60.0`
   - `position_reconciliation_deferral_max: int = 5`
   - `recently_reconciled_ttl_seconds: float = 60.0`
   - `reconcile_venue_has_more: bool = False`

2. Update `config/loaders.py` to read the new keys from YAML and construct
   `WalletSyncConfig` with them. Add validation:
   - `position_reconciliation_enabled` requires `wallet_sync_enabled`.
   - `data_api_lag_tolerance_seconds >= 0.0`.
   - Warn if `data_api_lag_tolerance_seconds < 30.0`.
   - `position_reconciliation_deferral_max >= 1`.
   - `recently_reconciled_ttl_seconds >= 0.0`.
   - Warn if `recently_reconciled_ttl_seconds < wallet_sync_poll_interval_seconds`.

3. Add hard validation in `_live_exec_engine_config()` in `runtime/guru_compose.py`
   (lines 83–107): if `position_reconciliation_enabled=True` and
   `generate_missing_orders` would be `False`, raise a configuration error. Since
   Tyrex never explicitly sets `generate_missing_orders`, this fires only if someone
   overrides it to `False`.

3. Add `ReconciliationAction` dataclass to `runtime/wallet_sync.py`.

4. Extend `WalletSyncResult` with:
   - `reconciliation_actions: list[ReconciliationAction]` (default empty list)
   - `reconciliation_sent_count: int = 0`
   - `reconciliation_deferred_count: int = 0`
   - `reconciliation_skipped_recently_reconciled: int = 0`

**Tests:**

- Config validation: `position_reconciliation_enabled=True` with
  `wallet_sync_enabled=False` raises.
- Floor enforcement: negative `data_api_lag_tolerance_seconds` raises.
- Floor enforcement: `position_reconciliation_deferral_max=0` raises.
- Default values: all new fields have expected defaults.
- Hard validation: `position_reconciliation_enabled=True` with
  `generate_missing_orders=False` raises configuration error in compose layer.

**DoD:**

- [x] `WalletSyncConfig` has 6 new fields with defaults.
- [x] `ReconciliationAction` dataclass is defined.
- [x] `WalletSyncResult` has 4 new fields.
- [x] Config loader reads and validates new keys.
- [x] `_live_exec_engine_config()` raises if reconciliation enabled with
  `generate_missing_orders=False`.
- [x] Existing tests pass unmodified.

---

## Step 2: Fact schema registration

**Depends on:** Nothing.

**Scope:**

1. Add `position_reconciliation` fact type to `_REQUIRED` in
   `reporting/schema/facts_v1.py`:

   ```python
   "position_reconciliation": frozenset({
       "cycle",
       "instrument_id",
       "venue_qty",
       "cache_qty",
       "diff_direction",
       "deferred",
       "defer_count",
       "reconciliation_sent",
   }),
   ```

2. Add golden payload for the new fact type in
   `tests/unit/test_reporting_facts_validation.py`.

**Tests:**

- Golden payload validates without error.
- Missing required key raises `FactValidationError`.
- Unknown fact type raises `FactValidationError` (existing test covers).

**DoD:**

- [x] `position_reconciliation` registered in `facts_v1.py`.
- [x] Golden payload test passes.
- [x] Fact emission and schema registration are in the same step (non-negotiable
  per project ground rule).

---

## Step 3: Diff algorithm and `_reconciliation_pass`

**Depends on:** Step 1 (config and data types).

**Scope:**

1. Implement `_build_venue_position_map(position_rows) -> dict[InstrumentId, Decimal]`
   as a method on `WalletSyncActor`. For each row:
   - Extract `asset` / `token_id` field.
   - Resolve to `InstrumentId` via cache scan (same pattern as
     `instrument_id_for_outcome_token` in `state_readers.py:52–66`).
   - Parse `size` to `Decimal`.
   - Aggregate by `InstrumentId`.

2. Implement `_build_cache_position_map() -> dict[InstrumentId, Decimal]`:
   - For each position in `self.cache.positions_open(venue=POLYMARKET_VENUE)`:
     `instrument_id → signed_decimal_qty()`.

3. Implement `_reconciliation_pass(position_rows) -> list[ReconciliationAction]`:
   - Build both maps.
   - Compute union of instrument IDs.
   - For each instrument, classify into match / stale-close / stale-partial /
     venue-has-more.
   - Apply race defenses (data API lag debounce, in-flight order check,
     recently-reconciled TTL).
   - Construct `PositionStatusReport` for actionable cases.
   - Return list of `ReconciliationAction` objects.

4. Construct `PositionStatusReport` objects using
   `nautilus_trader.execution.reports.PositionStatusReport`:
   - `account_id`: from `self.cache.account(POLYMARKET_VENUE).id` or synthetic.
   - `instrument_id`: the diffed instrument.
   - `position_side`: `LONG` if `venue_qty > 0`, else `FLAT`.
   - `quantity`: `Quantity(abs(venue_qty), instrument.size_precision)`.
   - `report_id`: `UUID4()`.
   - `ts_last` / `ts_init`: `self.clock.timestamp_ns()`.
   - `venue_position_id`: `None` (netting OMS).
   - `avg_px_open`: `None` (Data API doesn't provide this for position rows;
     engine uses fallback pricing).

**Tests:**

- Diff algorithm: 4 test cases (match, stale-close, stale-partial, venue-has-more).
- Venue map builder: handles missing/zero `size`, multiple outcomes for same condition.
- Cache map builder: sums positions correctly, handles flat positions.
- `PositionStatusReport` construction: all fields populated correctly.

**DoD:**

- [x] `_build_venue_position_map` implemented and tested.
- [x] `_build_cache_position_map` implemented and tested.
- [x] `_reconciliation_pass` returns correct actions for all 4 cases.
- [x] `PositionStatusReport` objects are well-formed (can be validated by engine).
- [x] Existing wallet-sync tests pass unmodified.

---

## Step 4: Race defenses

**Depends on:** Step 3 (diff algorithm).

**Scope:**

1. **Race B (Data API lag):** Before reconciling instrument X, check the cache
   `Position.ts_last` timestamp (`model/position.pxd:91–92`). If any position for
   that instrument has `ts_last` younger than `data_api_lag_tolerance_seconds` (default
   `60.0`), defer with reason `"position_recently_modified"`. This protects against the
   case where Nautilus just processed a real fill but the Data API hasn't caught up.

2. **Race C (In-flight orders):** Before sending a report for a stale-close or
   stale-partial case, query `self.cache.orders_open(instrument_id=iid)` and
   `self.cache.orders_inflight(instrument_id=iid)` for SELL-side orders. If
   pending sell qty >= delta, defer. Increment `defer_count`. If
   `defer_count >= position_reconciliation_deferral_max`, proceed anyway and
   log warning.

3. **Race E (Recently-reconciled TTL):** Check `_recently_reconciled[iid]` against
   `time.monotonic()`. Skip if within TTL.

4. **Race F (Concurrent timer):** Add `_cycle_in_progress` flag. `on_timer` skips
   if flag is set. Cleared in `finally` block of `_sync_cycle_wrapper`.

5. **Race G (Thread safety):** Capture event loop in `on_start` via
   `asyncio.get_running_loop()`. At end of `_sync_cycle_wrapper` (executor thread),
   call `self._event_loop.call_soon_threadsafe(self._apply_reconciliation_actions, actions)`.
   `_apply_reconciliation_actions` runs on the event-loop thread.

6. Add `stuck_deferral_count` property to `WalletSyncActor`:
   ```python
   @property
   def stuck_deferral_count(self) -> int:
       return sum(
           1 for count in self._deferred_reconciliations.values()
           if count >= self._wsconfig.position_reconciliation_deferral_max
       )
   ```

**Tests:**

- **Race B:** Position with `ts_last` younger than tolerance: deferred. Position with
  `ts_last` older than tolerance: action sent.
- **Race C:** In-flight SELL covering delta: deferred. SELL not covering delta: sent.
  Max deferrals reached: sent with warning.
- **Race E:** Reconciliation for instrument within TTL: skipped. After TTL: sent.
- **Race F:** Timer fires while cycle in progress: skipped.
- **Idempotence:** Same discrepancy across two cycles with action on first: second
  cycle sees quantities_match and takes no new action.

**DoD:**

- [x] All 5 race defenses implemented with correct logic.
- [x] `stuck_deferral_count` property returns correct count.
- [x] Each defense has at least one positive and one negative test case.
- [x] Race F: `_cycle_in_progress` cleared even on exception.

---

## Step 5: Thread-safe action application and timer refactor

**Depends on:** Step 3 and Step 4.

**Scope:**

1. Capture event loop in `on_start`:

   ```python
   def on_start(self) -> None:
       self._event_loop = asyncio.get_running_loop()
       self._start_mono = time.monotonic()
       self.run_in_executor(self._sync_cycle_wrapper)
       self.clock.set_timer(...)
   ```

   `asyncio.get_running_loop()` is correct: `on_start()` runs during
   `kernel.start_async()` while the asyncio loop is active in the current
   thread (`system/kernel.py:1036`).

2. Refactor `on_timer`:

   ```python
   def on_timer(self, event: Event) -> None:
       if self._cycle_in_progress:
           return
       self._cycle_in_progress = True
       self.run_in_executor(self._sync_cycle_wrapper)
   ```

3. Add `_sync_cycle_wrapper`:

   ```python
   def _sync_cycle_wrapper(self) -> None:
       try:
           result = self._sync_cycle()
           if result.reconciliation_actions:
               self._event_loop.call_soon_threadsafe(
                   self._apply_reconciliation_actions,
                   result.reconciliation_actions,
               )
       except Exception:
           _LOG.exception("event=wallet_sync_cycle_error component=wallet_sync")
       finally:
           self._cycle_in_progress = False
   ```

   `call_soon_threadsafe` schedules `_apply_reconciliation_actions` on the
   event-loop thread. No queue, no latency penalty.

4. Add `_apply_reconciliation_actions`:

   ```python
   def _apply_reconciliation_actions(
       self, actions: list[ReconciliationAction],
   ) -> None:
       for action in actions:
           if action.report is None:
               continue
           if self._wsconfig.position_reconciliation_shadow_mode:
               self._emit_reconciliation_fact(action, self._cycle_count, sent=False)
               continue
           self.msgbus.send(
               "ExecEngine.reconcile_execution_report",
               action.report,
           )
           self._recently_reconciled[action.instrument_id] = time.monotonic()
           self._reconciliation_count += 1
           self._deferred_reconciliations.pop(action.instrument_id, None)
   ```

5. Integrate `_reconciliation_pass` into `_sync_cycle`: call after discovery,
   store results in `WalletSyncResult.reconciliation_actions`.

**Tests:**

- `_apply_reconciliation_actions` calls `msgbus.send` with correct endpoint and report.
- Shadow mode: `_apply_reconciliation_actions` skips `msgbus.send` and emits fact with
  `reconciliation_sent=False`.
- Thread safety: mock `msgbus.send` is called from event-loop thread context.
- `_sync_cycle_wrapper` catches exceptions and clears `_cycle_in_progress`.

**DoD:**

- [x] `msgbus.send` is only called from the event-loop thread.
- [x] `_event_loop` captured via `asyncio.get_running_loop()` in `on_start`.
- [x] `call_soon_threadsafe` dispatches actions from executor to event loop.
- [x] `_cycle_in_progress` flag prevents concurrent cycles.
- [x] Exception in `_sync_cycle` does not leave flag stuck.
- [x] Shadow mode branch emits fact without sending to engine.

---

## Step 6: Fact emission for reconciliation actions

**Depends on:** Step 2 (schema) and Step 5 (action application).

**Scope:**

1. Emit `position_reconciliation` fact for each diff detected in `_reconciliation_pass`.
   Emit from the executor thread (fact emission is already done from executor thread
   in the existing `_emit_sync_fact`).

2. Add `_emit_reconciliation_fact` method:

   ```python
   def _emit_reconciliation_fact(self, action: ReconciliationAction, cycle: int) -> None:
       if self._fact_emit is None:
           return
       self._fact_emit("position_reconciliation", {
           "cycle": cycle,
           "instrument_id": str(action.instrument_id),
           "venue_qty": str(action.venue_qty),
           "cache_qty": str(action.cache_qty),
           "diff_direction": action.diff_direction,
           "deferred": action.deferred,
           "defer_count": action.defer_count,
           "reconciliation_sent": action.report is not None,
       })
   ```

3. Call `_emit_reconciliation_fact` for each action in `_reconciliation_pass`.

**Tests:**

- Fact emitted with correct keys for each diff_direction.
- Fact validates against `facts_v1.py` schema.
- No fact emitted when `_fact_emit` is None.

**DoD:**

- [x] `position_reconciliation` fact emitted for every diff (including deferrals).
- [x] Fact passes schema validation.
- [x] Fact emission and schema registration are in the codebase (Step 2 completed first).

---

## Step 7: Shadow-mode validation step

**Depends on:** Step 5 (action application) and Step 6 (fact emission).

**Scope:**

After the diff algorithm, race defenses, and fact schema are implemented but before
any `msgbus.send` call is wired live, ship the actor in observation-only mode.

1. The `position_reconciliation_shadow_mode` config key (default `true`) gates the
   `msgbus.send` call in `_apply_reconciliation_actions`. When `shadow_mode=true`:
   - The reconciliation pass runs normally (HTTP fetch, diff, race defenses).
   - `ReconciliationAction` objects are computed and dispatched via `call_soon_threadsafe`.
   - `_apply_reconciliation_actions` emits the `position_reconciliation` fact with
     `reconciliation_sent=false` for every actionable diff.
   - `msgbus.send` is **not** called — engine state is not mutated.

2. Deploy in production with `position_reconciliation_enabled=true` and
   `position_reconciliation_shadow_mode=true` (default). Operators validate emitted
   facts against real wallet activity for a defined period (recommended: one week).

3. A subsequent deployment flips `position_reconciliation_shadow_mode=false`, at
   which point the actor begins injecting `PositionStatusReport` objects into the
   engine.

**Rationale:** The previous round's two production bugs (missing argument, unregistered
fact type) both got past unit tests. Shadow mode provides an intermediate observation
step where the diff algorithm is exercised against real Data API quirks, real instrument
naming edge cases, and real timing patterns — without risking engine-state mutation.

**Temporary rollout tool:** `position_reconciliation_shadow_mode` is expected to be
removed once the actor's behavior is validated in production. Add a follow-up cleanup
item to remove the config key and the branching logic after production validation
completes.

**Tests:**

- Shadow mode on: `_apply_reconciliation_actions` emits fact with `reconciliation_sent=false`,
  does not call `msgbus.send`.
- Shadow mode off: `_apply_reconciliation_actions` calls `msgbus.send` and emits fact
  with `reconciliation_sent=true`.

**DoD:**

- [x] Shadow mode branch in `_apply_reconciliation_actions` implemented.
- [x] Facts emitted with `reconciliation_sent=false` in shadow mode.
- [x] No `msgbus.send` calls in shadow mode.
- [x] Deployed to production with shadow mode on.

---

## Step 8: Health source extension

**Depends on:** Step 4 (`stuck_deferral_count`).

**Scope:**

1. Add `stuck_deferral_count` to `WalletSyncHealthAdapter` protocol:

   ```python
   @property
   def stuck_deferral_count(self) -> int: ...
   ```

2. Add Rule 6.5 to `NautilusLiveExecutionHealthSource.snapshot()`:

   ```python
   # After existing rules 4-6 (stale, unresolvable):
   if ws.stuck_deferral_count > 0:
       return TradableStateHealthSnapshot(
           level=TradableStateHealth.DEGRADED_OMS,
           reason_code="position_reconciliation_stuck",
           observed_at_utc=now,
           framework_detail=...,
       )
   ```

3. Priority: stale/unresolvable (existing rules 4–6) win over stuck-deferral.
   Stuck-deferral only fires when sync is otherwise healthy but reconciliation
   can't proceed.

**Tests:**

- Stuck deferral count > 0 → `DEGRADED_OMS` / `position_reconciliation_stuck`.
- Stale + stuck deferral → `DEGRADED_OMS` / `wallet_sync_stale` (stale wins).
- No stuck deferrals → passes through to Rule 7 (HEALTHY) if no other issues.
- `stuck_deferral_count = 0` when no deferrals or all under max.

**DoD:**

- [x] `WalletSyncHealthAdapter` protocol includes `stuck_deferral_count`.
- [x] Health source evaluates Rule 6.5 at correct priority.
- [x] All existing health source tests pass unmodified.
- [x] New test cases for stuck-deferral scenarios.

---

## Step 9: Integration test (original failing scenario)

**Depends on:** Steps 1–8.

**Scope:**

End-to-end test that simulates the original failing scenario:

1. Set up `WalletSyncActor` with `position_reconciliation_enabled=True`,
   `position_reconciliation_shadow_mode=False`.
2. Mock 3 open positions in cache (to fill portfolio cap).
3. Mock Data API returning 0 positions for all 3 instruments (external close).
4. Run a sync cycle.
5. Verify `ReconciliationAction` objects created for all 3 instruments.
6. Simulate `call_soon_threadsafe` dispatch (call `_apply_reconciliation_actions`
   directly on the event-loop thread).
7. Verify `msgbus.send` called 3 times with `PositionStatusReport` objects.
8. Simulate engine processing: verify positions removed from cache.
9. Verify `NautilusDeploymentBudget.filled_polymarket_usd()` returns 0.
10. Verify next risk decision approves a new BUY order.

**Tests:**

- Integration test with mocked cache, msgbus, and deployment budget.
- Full cycle: detection → action → application → cache update → budget update.

**DoD:**

- [x] Integration test passes end-to-end.
- [x] Portfolio cap unblocked after reconciliation.
- [x] Deployment budget reflects updated position state.
- [x] All unit tests from Steps 1–8 still pass.

---

## Step 10: Config scenario for live testing

**Depends on:** Steps 1–8 (feature complete).

**Scope:**

1. Update `config/scenarios/wallet_sync_live/live_polymarket.yaml` to include:
   ```yaml
   position_reconciliation_enabled: true
   position_reconciliation_shadow_mode: true
   data_api_lag_tolerance_seconds: 60.0
   position_reconciliation_deferral_max: 5
   recently_reconciled_ttl_seconds: 60.0
   reconcile_venue_has_more: false
   ```

2. Update the scenario `README.md` with instructions for testing position
   reconciliation in shadow mode:
   - Deploy with shadow mode on.
   - Open 2-3 positions via the bot.
   - Manually sell 1 position on the Polymarket UI.
   - Inspect `position_reconciliation` facts in reporting artifacts.
   - Verify diff directions match actual wallet activity.
   - After validation, flip `position_reconciliation_shadow_mode: false`.
   - Observe logs for `event=position_reconciliation_sent`.
   - Verify deployment budget drops.
   - Verify next signal triggers a BUY.

**DoD:**

- [x] Config scenario updated with new keys (shadow mode on by default).
- [x] README includes shadow-mode validation and live testing instructions.

---

## Step 11: Documentation update

**Depends on:** Steps 1–10.

**Scope:**

1. Update `docs/implementation/venue_sync_truth/03_config.md` to include all 6 new
   position reconciliation config keys (including `position_reconciliation_shadow_mode`).

2. Update `docs/implementation/venue_sync_truth/02_components.md` to reference the
   new reconciliation pass in `WalletSyncActor`.

3. Add follow-up cleanup item: remove `position_reconciliation_shadow_mode` config key
   and branching logic after production validation completes.

**DoD:**

- [x] `03_config.md` lists all position reconciliation config keys.
- [x] `02_components.md` references the reconciliation extension.
- [x] Shadow-mode removal tracked as follow-up.
- [x] No stale documentation references.

---

## Migration order constraints

```
Step 1 ─────┐
Step 2 ─────┤
            ├─── Step 3 ──── Step 4 ──── Step 5 ──── Step 6 ──── Step 7 ──── Step 9
            │                                │
            │                                └──── Step 8
            │
Step 10 (after Steps 1–8)
Step 11 (after Steps 1–10)
```

- Steps 1 and 2 are independent and can be parallel.
- Step 3 requires Step 1 (needs config types and data structures).
- Step 4 requires Step 3 (extends diff algorithm with defenses).
- Step 5 requires Steps 3 and 4 (integrates diff + defenses into timer).
- Step 6 requires Steps 2 and 5 (needs schema + implementation).
- Step 7 requires Steps 5 and 6 (shadow mode validates diff + fact emission).
- Step 8 requires Step 4 (needs `stuck_deferral_count`).
- Step 9 requires Steps 1–8 (integration test needs shadow mode off path).
- Step 10 requires Steps 1–8 (config scenario deploys with shadow mode on).
- Step 11 can proceed alongside Steps 9–10.

**No step may leave the system in a state where partial reconciliation is active
but races aren't yet defended.** Steps 3 and 4 are separate for reviewability but
must be merged together — the diff algorithm is not safe without race defenses.
If merging is not practical, Step 3 must leave `position_reconciliation_enabled`
defaulting to `False` so the code path is not reachable.

**Shadow mode (Step 7) must be deployed before Step 9's live engine-mutation path.**
The default `position_reconciliation_shadow_mode=true` ensures that even if Steps 5–6
are deployed to production, no engine state is mutated until shadow mode is explicitly
disabled.
