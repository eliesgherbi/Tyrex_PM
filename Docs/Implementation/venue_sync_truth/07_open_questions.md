# 07 — Open Questions and Upstream Limitations

## OQ-1: Adapter position reports remain cache-scoped even with `use_data_api=true`

**Adapter code location:** `nautilus_trader/adapters/polymarket/execution.py`, method `generate_position_status_reports`.

**Issue:** When `use_data_api=true`, the adapter fetches bulk positions from the Data API (`/positions` for the user address). However, it then iterates **only** the instruments in `self._cache.instruments(venue=POLYMARKET)` to map the position data into `PositionStatusReport` objects:

```python
# execution.py, inside generate_position_status_reports with use_data_api path:
instruments = self._cache.instruments(venue=POLYMARKET_VENUE)
for instrument in instruments:
    token_id = get_polymarket_token_id(instrument.id)
    # ... look up token_id in the fetched position data
    # ... build PositionStatusReport
```

The Data API response is wallet-scoped (contains all positions), but the **output** is filtered to cached instruments. Positions on un-cached markets are fetched but discarded.

**Impact:** There is a window between "WalletSyncActor adds instrument to Cache" and "next position reconciliation cycle includes it." During this window, the position exists on the wallet but not in Nautilus state. This window is bounded by `position_check_interval_secs` (default 45s).

**Mitigation in this plan:** WalletSyncActor closes the cache gap proactively, making the window small. The continuous reconciliation loop handles the rest.

**Upstream fix:** The adapter should emit `PositionStatusReport` objects for all position data it fetches, not just for cached instruments. If the instrument is not in cache, the engine would need to handle the report by either: (a) loading the instrument (ideal), or (b) logging a warning with the condition_id and token_id. This would eliminate the cache-scope gap entirely.

**Status:** Out of scope for this plan. Documented for upstream contribution.

---

## OQ-2: Adapter WS message handling drops events for un-cached instruments

**Adapter code location:** `nautilus_trader/adapters/polymarket/execution.py`, methods `_handle_ws_order_msg` (line ~1282) and `_handle_ws_trade_msg` (line ~1420).

**Issue:** Both methods check `self._cache.instrument(instrument_id)`. If it returns `None`, the message is silently dropped with a warning log.

**Impact:** If a human places an order on a market that is not yet in cache (between wallet sync cycles), the WS event is lost. The order/fill will only be picked up on the next reconciliation cycle.

**Mitigation in this plan:** With wallet sync running at 15s intervals, the window for this is small. Once the instrument is in cache and WS is subscribed (via `_maintain_active_market` called during the next order report generation), subsequent events will be processed.

**Upstream fix:** Instead of dropping the message, the adapter should queue it and attempt to load the instrument via `InstrumentProvider.load_async(instrument_id)`. If successful, replay the queued message. This is a substantial change to the adapter's architecture.

**Status:** Out of scope. The polling-based approach is sufficient for the stated latency requirements.

---

## OQ-3: `get_orders()` py-clob API returns only active orders

**API behavior:** `ClobClient.get_orders()` returns only orders with an active/open status. Historical, filled, and canceled orders are not included.

**Impact on wallet sync:** The actor only discovers **currently resting** orders. If a human places and fills an order between poll cycles, the order phase is missed — but the **position** will be detected on the next positions poll. This is acceptable for deployment budget accuracy.

**Impact on order reconciliation:** Historical orders on newly-discovered markets are not recovered by the wallet sync actor. They are recovered (if within lookback) by the engine's `_check_orders_consistency` via `generate_order_status_reports`.

**Status:** Not a blocking issue. Documented for awareness.

---

## OQ-4: `_maintain_active_market` requires instrument_id with condition_id derivable

**Adapter code location:** `nautilus_trader/adapters/polymarket/execution.py:274`

```python
async def _maintain_active_market(self, instrument_id: InstrumentId) -> None:
    condition_id = get_polymarket_condition_id(instrument_id)
    if condition_id in self._active_markets:
        return
    # ... subscribe WS
    self._active_markets.add(condition_id)
```

**Observation:** The method subscribes by `condition_id`, not by individual `instrument_id`. A single condition_id subscription covers **both** outcomes (Yes and No tokens). So adding either outcome instrument to cache and triggering a reconciliation cycle for it will open the WS channel for the entire market.

**Implication for wallet sync:** When the Data API `/positions` returns a position on one outcome token, the wallet sync actor resolves and caches **that specific token's** instrument. The WS subscription opened via reconciliation covers the entire condition_id, so events for the other outcome token are also received. However, if the user also holds the other outcome token, the actor should resolve both tokens. The current Data API returns separate rows for each token, so both will be resolved.

**Status:** Not a problem. Documented for awareness.

---

## OQ-5: Thread safety of `Cache.add_instrument` from Actor context

**Question:** Is it safe to call `self.cache.add_instrument(instrument)` from within `WalletSyncActor._sync_cycle` which runs in an executor thread via `self.run_in_executor`?

**Corrected model (implementation-verified):** `Actor` does not have `create_task` — that method is on `LiveExecutionClient` only (`live/execution_client.py:157`). The `WalletSyncActor` dispatches its synchronous `_sync_cycle` method to an executor thread via `self.run_in_executor` (`actor.pxd:143`), which uses `asyncio.to_thread` internally. This means `Cache.add_instrument` is called from an **executor thread**, not the event loop thread.

**Evidence it is safe:**
1. `CacheInstrumentActivator.force_add_instrument` (used by `resolve_and_activate_by_condition_and_token`) acquires a `threading.Lock` (`guru_instrument_dynamic.py:249,278`) before calling `self._cache.add_currency(instrument.quote_currency)` and `self._cache.add_instrument(instrument)`.
2. The existing compose-time warmup calls this from the compose thread (before the event loop starts), demonstrating that `Cache.add_instrument` is used from non-event-loop threads in the existing codebase.
3. The lock serializes all cache mutations regardless of which thread initiates them.

**Conclusion:** Safe. The `CacheInstrumentActivator`'s `threading.Lock` is the correctness mechanism that makes executor-thread calls safe. This is not belt-and-suspenders — the lock is load-bearing for the `run_in_executor` path.

**Status:** Resolved — verified during implementation. The original claim about `create_task` running on the event loop was incorrect; the lock-based safety is what actually protects the call path.

---

## OQ-6: Data API `/positions` latency after venue-side action

**Question:** How quickly does the Polymarket Data API reflect a new position after a fill?

**Observation:** The Data API is not real-time. Based on Polymarket documentation and observed behavior, there can be a delay of several seconds to a minute between a fill occurring on the CLOB and the Data API reflecting the updated position size.

**Impact on wallet sync:** If a human places an order and it fills immediately, the wallet sync actor may not detect the position on its first poll after the fill. It will detect it on a subsequent poll.

**Mitigation:** The 15s default poll interval, combined with the Data API's propagation delay, means the worst-case discovery time is approximately `data_api_lag + poll_interval + reconciliation_interval` ≈ 30–90 seconds. For the stated objective (cap reopening), this is acceptable.

**Alternative:** Monitor py-clob `get_orders()` for fills (order transitions to `MATCHED` status). This is faster than the Data API but only covers orders, not direct position changes.

**Status:** Acceptable for current requirements. The operator should understand that wallet sync provides eventual consistency, not real-time mirroring.

---

## OQ-7: Rate limits on py-clob and Data API

**Question:** What are the rate limits for `get_orders()` and Data API `/positions`?

**Observation:** Polymarket's CLOB API documentation specifies rate limits (typically 10-30 req/s for authenticated endpoints). The Data API has looser limits. At 15s poll interval, the wallet sync actor makes 2 requests per cycle (1 positions + 1 orders) = ~8 req/min. This is well within typical limits.

**Risk:** If the poll interval is set aggressively low (e.g., 5s), the rate could reach ~24 req/min. This is still within normal limits but should be monitored.

**Mitigation:** Floor of 5.0s on `wallet_sync_poll_interval_seconds` in config validation.

**Status:** Low risk. Monitor in production.

---

## OQ-8: Adapter does not call `_update_account_state` after reconciliation-driven fills

**Adapter code location:** `nautilus_trader/adapters/polymarket/execution.py`

**Observation:** `_update_account_state()` (which calls `generate_account_state` → updates Portfolio account) is called:
1. In `_connect()` — once at startup.
2. In `_handle_ws_trade_msg` — after finalized trades received via WS.

It is **NOT** called after reconciliation-driven fills (fills discovered through `generate_fill_reports` during the engine's position consistency check).

**Impact:** After a manual trade is reconciled through the periodic position check, the account balance in Portfolio may not update until the next WS trade event or manual refresh. This affects `NautilusAccountSnapshotProvider.snapshot()` and therefore the capital gate.

**Mitigation in this plan:** The `ClobAllowanceStateProvider` reads directly from py-clob HTTP, independent of the adapter's account state. The `DefaultCapitalStateProvider` merges both sources, preferring Nautilus but falling back to py-clob. So the capital gate is not entirely blind — the py-clob source provides a backstop.

**Upstream fix:** The adapter should call `_update_account_state()` after processing reconciliation-driven fill reports.

**Status:** Out of scope for this plan. Low severity because the py-clob backstop exists.
