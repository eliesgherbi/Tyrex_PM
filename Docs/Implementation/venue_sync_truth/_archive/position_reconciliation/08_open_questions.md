# Position Reconciliation — Open Questions

## OQ-1: Cross-thread dispatch from executor to event loop

**Status:** Resolved — `asyncio.get_running_loop()` + `loop.call_soon_threadsafe()`.

**Problem:** `_sync_cycle` runs in an executor thread via `run_in_executor`.
`msgbus.send` is not thread-safe (`MessageBus` docstring: *"This message bus is not
thread-safe and must be called from the same thread as the event loop"* —
`component.pyx:2221–2224`). The actor needs to dispatch reconciliation actions from the
executor thread to the event-loop thread.

**Chosen approach:** `loop.call_soon_threadsafe(callback)`.

1. During `on_start` (which runs on the event-loop thread), capture the loop:
   `self._event_loop = asyncio.get_running_loop()`.

2. At the end of `_sync_cycle_wrapper` (executor thread), schedule action application:
   `self._event_loop.call_soon_threadsafe(self._apply_reconciliation_actions, actions)`.

3. `_apply_reconciliation_actions` runs on the event-loop thread and is the only place
   `msgbus.send` is invoked. No latency penalty — actions are applied on the next event
   loop iteration, not the next timer tick.

**Why `asyncio.get_running_loop()` is correct in `on_start()`:**

`on_start()` is called from `Actor._start()` (`actor.pyx:1203–1204`), which is called
from `Component.start()` → `_trigger_fsm(action=self._start)` — all synchronous on
the caller's thread (`component.pyx:2137–2155`). The caller is `Trader._start()` →
`actor.start()` (`trading/trader.py:250–270`), called from `kernel.start_async()` →
`self._trader.start()` (`system/kernel.py:1036`). Since `start_async` is a coroutine
running on the event loop, `on_start()` executes with the asyncio loop running in the
current thread. `asyncio.get_running_loop()` returns the correct loop.

**Why `call_soon_threadsafe` works:**

`call_soon_threadsafe` is a stable public asyncio API (Python docs: *"Thread-safe variant
of `call_soon()`. Must be used to schedule callbacks from another thread."*). It does not
go through `ActorExecutor` — it schedules directly on the asyncio event loop. The
callback executes on the event-loop thread on the next loop iteration.

**Rejected alternative:** `queue.Queue` with timer-tick draining. This adds up to one
full poll interval (~15s) of avoidable latency per reconciliation. The cap stays pinned
for an extra cycle every time — a regression on the user-facing guarantee.

**Rejected alternative 2:** `self._executor._loop.call_soon_threadsafe(...)`. This uses
`ActorExecutor._loop` (`executor.py:95`), a private attribute. It would work but couples
to internal implementation details unnecessarily, since the loop reference can be captured
directly via `asyncio.get_running_loop()` in `on_start()`.

## OQ-2: `avg_px_open` in PositionStatusReport

**Status:** Verified — optional field, impacts reconciliation price quality.

`PositionStatusReport.__init__` accepts `avg_px_open: Decimal | None = None`
(`execution/reports.py:905`). When populated, the engine's `_create_position_reconciliation_report`
uses it to calculate a reconciliation price via `calculate_reconciliation_price()`
(`live/execution_engine.py:2667–2673`). When `None`, the engine falls back to the last
quote tick or the position's current avg_px.

The Data API `/positions` endpoint returns a `size` field but **may not** return
`avg_px_open` or equivalent pricing data for all positions. This needs verification
during implementation.

**Impact:** Without `avg_px_open`, reconciliation-origin closes will have approximate PnL.
For close-only reconciliation (the primary use case), this is acceptable — the position
is being zeroed out, and the exact close price is unknown anyway (external action).

**Recommendation:** Set `avg_px_open=None` in the initial implementation. If the Data API
provides price data (e.g., `average_price` or `cost_basis` fields), add it in a
follow-up. The operator-facing impact of approximate PnL is documented in
`00_overview.md` §Known accuracy trade-offs.

## OQ-3: `Cache.positions_open` thread safety

**Status:** Partially verified — reads are likely safe, but not guaranteed.

`_sync_cycle` runs in an executor thread. `_reconciliation_pass` calls
`self.cache.positions_open(venue=POLYMARKET_VENUE)` to build the cache position map.
This is a read-only call. The existing wallet-sync code already reads from cache
(`self.cache.instruments(venue=_POLYMARKET_VENUE)`) in the executor thread, and the
discovery pass calls `CacheInstrumentActivator` which mutates cache under a
`threading.Lock`.

`positions_open` (`cache.pyx:4185+`) iterates `_index_positions_open` and lookups
in `_cached_positions`. In CPython, dictionary/set iteration is protected by the GIL
for simple read operations. No crashes are expected, but stale reads are possible
(position state may change between the read and the reconciliation diff computation).

**Impact:** A stale read means the reconciliation pass might see a position that was
already closed by a real fill arriving between the cache read and the diff. The race
defense (data API lag debounce) handles this: the discrepancy would appear on one
cycle but resolve on the next, and the debounce prevents acting on a single-cycle
mismatch.

**Recommendation:** Accept the current design. Document that cache reads from the
executor thread are best-effort snapshots. The debounce defense makes stale reads
harmless.

## OQ-4: `generate_missing_orders` dependency

**Status:** Resolved — hard startup check in compose layer.

Position reconciliation through `PositionStatusReport` → `_reconcile_position_report_netting`
requires `generate_missing_orders=True` (engine attribute, `live/execution_engine.py:189`).
If `False`, the engine logs a warning and returns without generating the synthetic order
(`live/execution_engine.py:2362–2367`). The reconciliation silently becomes a no-op.

`generate_missing_orders` defaults to `True` (`live/config.py:201`) and is a
`LiveExecEngineConfig` parameter. Tyrex never explicitly sets it (the default is used).

**Chosen approach:** Hard validation check at compose time. `_live_exec_engine_config()`
in `runtime/guru_compose.py` (lines 83–107) is the only Tyrex function that constructs
`LiveExecEngineConfig`. It has access to both `RuntimeSettings` (which carries
`position_reconciliation_enabled`) and the kwargs being built for `LiveExecEngineConfig`.
If `position_reconciliation_enabled=True` and the resulting config would have
`generate_missing_orders=False`, the function raises a configuration error and refuses
to start.

This prevents the same class of silent-no-op bug as the unregistered fact type from
the previous round: production-affecting, hard to debug, easy to prevent at startup.

## OQ-5: `account_id` for PositionStatusReport construction

**Status:** Resolved — verified. Account is guaranteed in cache before
`first_sync_complete` can become `True`.

`PositionStatusReport.__init__` requires an `AccountId` (`execution/reports.py:880`).
The actor reads it via `self.cache.account_for_venue(POLYMARKET_VENUE).id`.

**Verification:** The Polymarket account is registered in cache during adapter startup,
which completes *before* the actor can start:

1. `PolymarketExecutionClient.__init__` sets `account_id` and calls
   `self._set_account_id(account_id)` (`adapters/polymarket/execution.py:171–173`).
2. `PolymarketExecutionClient._connect` calls `self._update_account_state()` (line 243)
   which invokes `self.generate_account_state(...)` (lines 294–317), publishing account
   state to the system.
3. `_connect` then calls `self._await_account_registered()` (line 243), which polls
   `self._cache.account(self.account_id)` until the account is present
   (`live/execution_client.py:525–558`).
4. The execution client is not considered connected until `_connect` succeeds and
   `_set_connected(True)` runs (`live/execution_client.py:231–241`).
5. `kernel.start_async()` calls `_connect_clients()` then
   `await self._await_engines_connected()` (`system/kernel.py:1017–1036`), which waits
   until the exec client reports connected.
6. Only after engine connection does `self._trader.start()` run (line 1036), which
   starts the `WalletSyncActor` via `actor.start()` → `on_start()`.
7. `first_sync_complete` can only become `True` after the actor starts and completes
   at least one successful sync cycle — strictly after the adapter has connected and
   registered the account.

**Conclusion:** `self.cache.account_for_venue(POLYMARKET_VENUE)` is guaranteed to return
a non-`None` value by the time the reconciliation pass runs (which is gated by
`first_sync_complete`). No per-cycle defense or fallback needed.

## OQ-6: Data API `/positions` payload structure

**Status:** Partially verified from existing code.

The existing `_fetch_wallet_positions` calls `fetch_wallet_position_rows` from
`guru_cache_warmup.py`. The returned rows are dictionaries with fields like
`conditionId`/`condition_id`, `asset`/`token_id`, and presumably `size` or equivalent.

The exact field name for position quantity needs verification during implementation.
Candidates from the Polymarket Data API:

- `size` — number of shares (as a string decimal).
- `rawSize` — raw size before any normalization.

If the field name differs, the venue map builder needs to accommodate it.

**Recommendation:** During Step 3 implementation, inspect actual API responses (or the
`fetch_wallet_position_rows` implementation) to confirm the field name. Add a fallback
chain similar to the existing `conditionId` / `condition_id` pattern.

## OQ-7: Interaction with engine's periodic `_check_positions_consistency`

**Status:** Analyzed — complementary, not conflicting.

The engine's `_continuous_reconciliation_loop` includes periodic position checks
(`_check_positions_consistency`, `live/execution_engine.py:901+`). This queries the
adapter's `generate_position_status_reports` and runs the same
`_reconcile_position_report_netting` pipeline.

Two reconciliation sources running concurrently are safe due to idempotence, but they
may produce redundant work:

1. Both detect the same discrepancy.
2. The first to act synthesizes the close.
3. The second sees quantities already match and takes no action.

However, the engine's position check also calls `_query_and_find_missing_fills`, which
can find **real** trade records from the adapter. If a real trade is found, it provides
exact pricing (unlike the WalletSyncActor's synthetic approach with approximate pricing).

**Recommendation:** Accept the overlap. The WalletSyncActor provides the **fast path**
(Data API lag is typically 10-30s; the adapter's periodic check may lag more because it
fetches all positions). The engine's check provides the **accurate path** (real trade
data with exact pricing). If both act, the first one wins and the second is a no-op.

Document that operators can adjust `position_check_interval_secs` (engine config) and
`wallet_sync_poll_interval_seconds` independently to control how aggressively each
source runs.

## OQ-8: Position with multiple open orders (BUY and SELL simultaneously)

**Status:** Needs careful thought during Step 4.

If an instrument has both BUY and SELL orders in-flight simultaneously (e.g., a BUY to
add and a SELL to partially reduce), the in-flight order check (Race C defense) only
counts SELL-side orders against the delta. But the BUY orders would increase the expected
cache position, potentially masking a real close.

Example:
- Cache: 100 shares
- In-flight BUY for 20 shares
- In-flight SELL for 50 shares
- Venue: 70 shares (50 were sold externally, 20 were bought)
- Delta: 100 - 70 = 30 (cache > venue)
- In-flight sell covers 50 ≥ 30 → deferred

This deferral is correct: the real fills for both orders haven't arrived yet. When they
do, cache will update to 70 (100 + 20 - 50), matching venue.

**Recommendation:** The SELL-only check for Race C is correct. BUY-side in-flight orders
don't affect the "venue has less" direction of reconciliation. No code change needed,
but add a test case for this scenario.

## OQ-9: Multi-position per instrument (netting vs hedging)

**Status:** Verified — Polymarket uses netting.

Polymarket uses netting OMS (no `venue_position_id`), so
`Cache.positions_open(instrument_id=iid)` can return multiple position objects for the
same instrument (e.g., if the position was partially filled across multiple orders).
`_reconcile_position_report_netting` already handles this by summing
`signed_decimal_qty()` across all positions (`live/execution_engine.py:2350–2353`).

The WalletSyncActor's cache position map builder should also sum across multiple
positions per instrument, matching the engine's behavior.

**Recommendation:** Use `sum(p.signed_decimal_qty() for p in cache.positions_open(instrument_id=iid))`
in `_build_cache_position_map`, not just `positions[0].signed_decimal_qty()`.

## OQ-10: `PositionStatusReport.signed_decimal_qty` computation

**Status:** Verified.

`PositionStatusReport.__init__` computes `signed_decimal_qty` from `position_side` and
`quantity` (`execution/reports.py:900–905`):

```python
self.signed_decimal_qty = (
    -self.quantity.as_decimal()
    if position_side == PositionSide.SHORT
    else self.quantity.as_decimal()
)
```

For a full close: `position_side=FLAT`, `quantity=Quantity(0, ...)` →
`signed_decimal_qty = Decimal(0)`. The engine's netting reconciliation sees
`position_signed_decimal_qty > 0` vs `report.signed_decimal_qty == 0` →
discrepancy → synthetic SELL.

For a partial reduce: `position_side=LONG`, `quantity=Quantity(venue_qty, ...)` →
`signed_decimal_qty = venue_qty`. Engine sees `cache_qty > venue_qty` →
synthetic SELL for the diff.

**Verified correct.** No action needed.
