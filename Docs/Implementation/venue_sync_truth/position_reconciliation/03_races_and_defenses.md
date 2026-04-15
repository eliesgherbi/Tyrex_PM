# Position Reconciliation — Races and Defenses

## Race A: Real fill in flight

**Scenario:** A strategy has submitted a BUY order. The fill hasn't arrived yet. Meanwhile,
the Data API already shows the increased position. The reconciliation pass sees
`venue_qty > cache_qty` and might try to synthesize an opening fill.

**Defense:** This is the "venue-has-more" case in the diff algorithm. By default
(`reconcile_venue_has_more=False`), the actor takes **no action** when venue > cache.
The real fill will arrive through the normal WebSocket/polling path and update the cache.

Even if `reconcile_venue_has_more=True`, the engine's reconciliation pipeline handles
this safely: it queries `generate_fill_reports` from the adapter, which returns the real
trade with its real `TradeId`. The engine deduplicates by `TradeId` — if the real fill
arrives later, it's recognized as already processed and dropped.

**Config knob:** `reconcile_venue_has_more` (default `False`).

**Failure mode if defense is wrong:** If `reconcile_venue_has_more=True` and the Data API
shows the position before the real fill arrives, the engine may synthesize a
RECONCILIATION fill with an approximate price. When the real fill later arrives, the engine
sees the order is already fully filled and drops the real fill (logged as
"ignoring duplicate fill"). PnL accounting uses the synthetic price instead of the real
price. This is cosmetically wrong but not unsafe — the position state is correct.

## Race B: Data API lag

**Scenario:** Nautilus just received a real fill (BUY or SELL) and updated the cache
Position object, but the Data API `/positions` endpoint hasn't caught up yet and still
shows the pre-fill quantity. The reconciliation pass sees a discrepancy and might
synthesize a duplicate close/reduction that the real fill already handled.

This is the primary race: the Data API can lag by "several seconds to a minute" after
a venue-side action (documented in `docs/implementation/venue_sync_truth/07_open_questions.md`
OQ-6).

**Defense:** Time-since-cache-mutation debounce. Before reconciling instrument X, the
actor checks the cache `Position` object's `ts_last` field — the UNIX timestamp
(nanoseconds) of the most recent event on that position (`model/position.pxd:91–92`,
docstring: *"UNIX timestamp (nanoseconds) when the last event occurred"*).

If any position for that instrument has `ts_last` younger than
`data_api_lag_tolerance_seconds` ago, the reconciliation is **deferred** with reason
`"position_recently_modified"`.

Implementation:

```python
now_ns = self.clock.timestamp_ns()
tolerance_ns = int(self._wsconfig.data_api_lag_tolerance_seconds * 1e9)

for pos in self.cache.positions_open(instrument_id=iid):
    if (now_ns - pos.ts_last) < tolerance_ns:
        # Position was modified recently — Data API may not have caught up
        self._deferred_reconciliations[iid] = defer_count + 1
        break  # defer this instrument
```

This protects the actual race: Nautilus just received a real fill and updated the
position, but the Data API hasn't reflected it yet. The cycle-count approach used
previously doesn't protect this case because the API can stay stale for the full poll
interval.

**Config knob:** `data_api_lag_tolerance_seconds` (default `60.0`). This matches the
documented upper bound of Data API propagation lag (OQ-6: "several seconds to a minute").
A value below `30.0` triggers a config-load warning because it may not cover typical
Data API lag.

The deferral counter still applies: if the discrepancy persists past
`position_reconciliation_deferral_max` cycles despite the position not being recently
modified, the reconciliation proceeds anyway (same as Race C's max-deferral override).

**Failure mode if defense is wrong:** If `data_api_lag_tolerance_seconds` is set too low
(below actual Data API lag), the actor may act on stale API data and send a
`PositionStatusReport` that contradicts a fill already in cache. The engine's
`_reconcile_position_report_netting` (`live/execution_engine.py:2345–2359`) re-reads
`Cache.positions_open(instrument_id=...)` at call time — by which point the real fill
has updated cache — so it sees `quantities_match == True` and takes no action. This is
a second-level safety net: the engine always uses current cache state, not the actor's
snapshot.

## Race C: Partial reduction with in-flight orders

**Scenario:** Cache shows 100 shares. An in-flight SELL order for 50 shares is pending.
Data API shows 50 shares (the sell has cleared on-chain). The reconciliation sees
`venue_qty=50, cache_qty=100` — a delta of 50 that matches the real pending sell.

**Defense:** Before sending a `PositionStatusReport`, the actor checks
`Cache.orders_inflight(instrument_id=...)` and `Cache.orders_open(instrument_id=...)` for
SELL orders. If the sum of pending SELL `leaves_qty` on the instrument equals or exceeds
the detected delta, the reconciliation is **deferred**.

Specifically:

```python
inflight_sell_qty = sum(
    order.leaves_qty.as_decimal()
    for order in self.cache.orders_open(instrument_id=iid)
    if order.side == OrderSide.SELL
) + sum(
    order.leaves_qty.as_decimal()
    for order in self.cache.orders_inflight(instrument_id=iid)
    if order.side == OrderSide.SELL
)

if inflight_sell_qty >= abs(delta):
    # Defer — the real fill may be in transit
    self._deferred_reconciliations[iid] = defer_count + 1
    continue
```

If the deferral persists for `position_reconciliation_deferral_max` cycles (default 5),
the reconciliation proceeds anyway: the order is likely stale / stuck, and the venue truth
should override. This is surfaced as a `position_reconciliation_stuck` health degradation.

**Config knob:** `position_reconciliation_deferral_max` (default `5`).

**Failure mode if defense is wrong:** If a real SELL fill was actually lost (WebSocket
dropped it, adapter bug), the 5-cycle deferral adds ~75s delay before reconciliation acts.
During this window, the deployment budget overstates the filled position. This is
conservative (blocks new buys rather than allowing excess exposure).

## Race D: Opening discrepancy (venue-has-more while cache is zero)

**Scenario:** An external buy was made on the Polymarket UI. Nautilus has no record of
this position. Data API shows 50 shares. Cache shows 0.

**Defense:** Same as Race A — this is a "venue-has-more" case. Default behavior is no-op.

When `reconcile_venue_has_more=True`, the engine receives the `PositionStatusReport` and
runs netting reconciliation. It sees `cache_qty=0, venue_qty=50` → `diff=50 BUY`. The
engine creates a synthetic EXTERNAL/RECONCILIATION MARKET BUY order, filled at the
venue-reported `avg_px_open` (if available from the `PositionStatusReport`) or falls back
to the last quote. The position appears in cache with the reconciled entry price.

**Important:** The `PositionStatusReport` constructor accepts `avg_px_open` as an optional
`Decimal` parameter (`execution/reports.py:905`). When building the report, the actor
should populate this from the Data API's position payload if available, to give the engine
a better reconciliation price.

**Config knob:** `reconcile_venue_has_more` (default `False`).

**Failure mode if defense is wrong:** If `reconcile_venue_has_more=True` and the position
was actually opened by Nautilus but the fill is in transit, a duplicate position could
briefly appear. The engine's trade_id deduplication and the `_find_matching_cached_order`
check mitigate this.

## Race E: Synthetic close in flight (reconciliation re-trigger)

**Scenario:** Cycle N sends a `PositionStatusReport` triggering a synthetic close. The
engine processes it and closes the position in cache. Cycle N+1 runs before the Data API
has been re-fetched — it sees the same venue state and the same (now absent) cache
position. Since cache is now flat and venue is also flat (or venue is 0), there's no
discrepancy.

**Defense:** The diff algorithm handles this naturally — if venue_qty == 0 and cache has
no open position, there's no diff to act on. The match case (both zero) is a no-op.

However, a subtler variant: the engine's synthetic close may not have fully propagated
through cache by the time the next executor-thread `_sync_cycle` reads cache state. The
`recently_reconciled_ttl_seconds` config (default 60s) prevents re-reconciliation of an
instrument within the TTL window. The TTL is set after a successful `msgbus.send` call.

**Config knob:** `recently_reconciled_ttl_seconds` (default `60.0`).

**Failure mode if defense is wrong:** If the TTL is too short and cache/engine processing
is slow, a second reconciliation report could be sent for an instrument already being
processed. The engine handles this gracefully — `_reconcile_position_report_netting` would
see `quantities_match == True` (if the first report has been fully processed) or would
generate a second identical synthetic order (which `_find_matching_cached_order` catches
and skips). No unsafe state.

## Race F: Concurrent timer callbacks

**Scenario:** The timer fires while a previous `_sync_cycle` is still running in the
executor. Two cycles run concurrently, both detect the same discrepancy, both return
reconciliation actions.

**Defense:** The actor tracks whether a cycle is in progress via a `_cycle_in_progress`
flag (set at the start of `on_timer`, cleared in `_on_sync_complete`). If the flag is
set, the timer callback skips the cycle.

```python
def on_timer(self, event: Event) -> None:
    if self._cycle_in_progress:
        return
    self._cycle_in_progress = True
    self.run_in_executor(self._sync_cycle)
```

**Failure mode:** If the flag is never cleared due to an exception in `_sync_cycle`,
subsequent cycles are permanently skipped. Defense: wrap `_sync_cycle` in try/finally
to always clear the flag.

## Race G: `msgbus.send` thread safety

**Scenario:** `_sync_cycle` runs in an executor thread. If it called `msgbus.send`
directly, the engine's `reconcile_execution_report` handler would execute in the executor
thread. The engine's internal state and cache mutations are not designed for concurrent
thread access. The `MessageBus` docstring explicitly states: *"This message bus is not
thread-safe and must be called from the same thread as the event loop"*
(`component.pyx:2221–2224`).

**Defense:** The reconciliation pass in `_sync_cycle` (executor thread) only **collects**
`ReconciliationAction` objects. At the end of `_sync_cycle_wrapper`, the executor thread
calls `self._event_loop.call_soon_threadsafe(self._apply_reconciliation_actions, actions)`
to schedule action application on the event-loop thread.

`call_soon_threadsafe` is a stable public `asyncio` API (Python docs: *"Thread-safe
variant of call_soon(). Must be used to schedule callbacks from another thread."*). It
does not go through `ActorExecutor`; it goes directly through the standard asyncio event
loop.

The event loop reference is captured during `on_start` (which runs on the event-loop
thread — `Component.start()` → `_trigger_fsm(action=self._start)` →
`Actor._start()` → `on_start()`, all synchronous on the caller's thread;
`component.pyx:2137–2155`):

```python
def on_start(self) -> None:
    self._event_loop = asyncio.get_running_loop()
    # ...
```

`asyncio.get_running_loop()` is the correct call here: `on_start` executes during
`kernel.start_async()` → `trader.start()` → `actor.start()` while the asyncio event
loop is running in the current thread (`system/kernel.py:1036`).

**Failure mode:** None under normal operation. `call_soon_threadsafe` is documented as
safe for cross-thread scheduling. If the event loop has been stopped (shutdown), the call
raises `RuntimeError` — but `on_stop` cancels the timer before that point, preventing
new cycles.

## Summary table

| Race | Trigger | Defense | Config knob | Failure mode |
|------|---------|---------|-------------|--------------|
| A | Real fill in flight (venue > cache) | No-op by default | `reconcile_venue_has_more` | Cosmetic PnL if enabled |
| B | Data API lag | Defer if Position.ts_last < tolerance | `data_api_lag_tolerance_seconds` | Engine re-reads cache at send time (safe) |
| C | Partial reduce + in-flight sells | Defer if sell qty covers delta | `position_reconciliation_deferral_max` | ~75s delay in worst case |
| D | External open (venue > cache, cache=0) | Same as A | `reconcile_venue_has_more` | Same as A |
| E | Synthetic close re-trigger | Recently-reconciled TTL | `recently_reconciled_ttl_seconds` | Engine handles gracefully |
| F | Concurrent timer | `_cycle_in_progress` flag | — | Flag cleared in finally |
| G | Thread safety on msgbus | `call_soon_threadsafe` from executor thread | — | None under normal operation |
