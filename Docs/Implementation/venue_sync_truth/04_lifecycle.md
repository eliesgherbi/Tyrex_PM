# 04 — Lifecycle

## Startup ordering

The target startup sequence integrates wallet sync into the existing flow:

```
1. load YAML config → RuntimeSettings, RiskSettings, StrategySettings
2. build_guru_trading_node (compose)
   a. ensure_polymarket_l2_env_from_pk_if_missing
   b. construct TradingNodeConfig (load_state=False, save_state=False)
   c. TradingNode(config=cfg)
   d. add_data_client_factory, add_exec_client_factory
   e. construct state readers, deployment budget, capital provider
   f. [EXISTING] warm_polymarket_cache_from_wallet_positions  → seeds Cache
   g. [EXISTING] warm_polymarket_cache_from_guru_activity     → seeds Cache
   h. construct GuruInstrumentDynamicController (now always for live)
   i. [NEW] construct WalletSyncActor
   j. construct strategy, risk policy, readiness gate
   k. [NEW] pass wallet_sync_ready to readiness gate + health source
   l. register actors + strategy on node.trader
3. node.build()  → triggers exec client _connect → WS for cached instruments
4. StartupReadinessCoordinator.start_background()  → polls readiness gate
5. node.run()  → blocking; starts the event loop
   a. actors' on_start() called by Nautilus
   b. WalletSyncActor.on_start() dispatches first sync cycle via run_in_executor
   c. exec engine startup reconciliation runs → _startup_reconciliation_event set
   d. continuous reconciliation loop begins
```

### Critical ordering constraint

The readiness gate must wait for **both**:

1. `_startup_reconciliation_event.is_set()` (Nautilus exec engine reconciliation finished) — existing check via `NautilusLiveExecutionHealthSource`.
2. `WalletSyncActor.first_sync_complete` — first full poll cycle done.

These happen concurrently on the event loop. The startup reconciliation runs during `node.build()` or early `node.run()` (before the continuous loop starts). The WalletSyncActor's `on_start` fires when the Nautilus trader starts its actors, which is also during `node.run()`. Both must complete before the readiness gate reports READY.

**Why this order is safe:** The wallet sync actor adds instruments to `Cache` during its first cycle. Any instruments it adds **after** startup reconciliation has already run will be picked up by the **continuous** reconciliation loop on its next interval. The important thing is that the readiness gate blocks trading until at least one full wallet scan has completed — ensuring the deployment budget has a complete picture before any order is evaluated.

### Readiness gate integration

```
StartupReadinessGate.evaluate()
  §1 shadow immediate → READY (no change)
  §2 exec_clients_connected → NOT_READY if false (no change)
  §3 capital_gate → NOT_READY if snapshot fails (no change)
  §4 [NEW] wallet_sync_ready → NOT_READY if WalletSyncActor exists and first_sync_complete is False
      reason: "startup_wallet_sync_pending"  (if deadline not exceeded)
      reason: "startup_wallet_sync_timeout"  (if startup_deadline_seconds exceeded)
  §5 health_source → UNKNOWN_BOOTSTRAP / DEGRADED_OMS / DIVERGENT_PERSISTENT / HEALTHY (enhanced)
  §6 instrument_readiness → NOT_READY if policy fails (no change)
```

The wallet sync check is placed **before** the health source check because health should not report HEALTHY until we know the cache is populated. This is also why the health source is enhanced: `NautilusLiveExecutionHealthSource` reports `UNKNOWN_BOOTSTRAP` when `first_sync_complete` is False and deadline is not exceeded, or `DEGRADED_OMS` on timeout — even if the engine's `_startup_reconciliation_event` is already set. This replaces the weak signal ("reconciliation pass finished") with a meaningful one ("reconciliation finished AND wallet instruments are loaded"). See `02_components.md` for the full health source rule set covering both startup and steady-state failures.

## Continuous operation

After startup:

```
every wallet_sync_poll_interval_seconds (default 15s):
  WalletSyncActor._sync_cycle()
    1. fetch wallet positions (Data API /positions)
    2. fetch wallet open orders (py-clob get_orders())
    3. build set of condition_ids from both
    4. diff against Cache instruments
    5. for each missing condition_id + token_id:
       a. resolve via GuruInstrumentDynamicController.resolve_and_activate_by_condition_and_token
       b. if success: log, increment counter
       c. if failure: log with detail, skip
    6. emit wallet_sync fact

every open_check_interval_secs (default 20s):
  LiveExecutionEngine._check_orders_consistency()
    → calls adapter.generate_order_status_reports (cache-scoped)
    → now covers all wallet instruments because WalletSyncActor keeps Cache populated
    → also calls _maintain_active_market for each order report (execution.py:554)

every position_check_interval_secs (default 45s):
  LiveExecutionEngine._check_positions_consistency()
    → calls adapter.generate_position_status_reports (cache-scoped)
    → now covers all wallet instruments
```

### How a newly discovered instrument gets fully integrated

1. **T=0:** WalletSyncActor discovers position/order on condition_id `XYZ` not in Cache.
2. **T≈0.5s:** Actor resolves via CLOB API → `parse_polymarket_instrument` → `Cache.add_instrument`.
3. **T≈0.5s:** The adapter's `_active_markets` set does NOT yet contain this condition_id. No WS subscription yet.
4. **T ≤ open_check_interval (20s):** Engine's `_check_orders_consistency` fires → calls `adapter.generate_order_status_reports` → adapter iterates `self._cache.instruments(venue=POLYMARKET)` → now includes `XYZ` → fetches orders → for each order, calls `_maintain_active_market(instrument_id)` → opens WS channel → returns order status reports → engine reconciles into Cache.
5. **T ≤ position_check_interval (45s):** Engine's `_check_positions_consistency` fires → calls `adapter.generate_position_status_reports` → now includes `XYZ` → fetches position → engine reconciles.
6. **T ≤ max(20, 45)s:** Both orders and positions for `XYZ` are reconciled into Cache/Portfolio. Deployment budget reads correct state.

### Latency bounds

The total time from a venue-side action to the deployment budget reflecting it has three additive components:

1. **Data API propagation lag:** The Polymarket Data API (`/positions`) is not real-time. Observed delays range from a few seconds to ~60 seconds after a fill (see `07_open_questions.md` OQ-6).
2. **Wallet sync poll interval:** Up to `wallet_sync_poll_interval_seconds` (default 15s) before the actor discovers the new state.
3. **Engine reconciliation interval:** Up to `max(open_check_interval_secs, position_check_interval_secs)` (default max(20, 45) = 45s) before the engine reconciles the newly cached instrument into Cache/Portfolio.

| Scenario | Typical | Upper bound (p99) |
|----------|---------|-------------------|
| Data API current, happy path | ~30 seconds | — |
| Data API lagging, all intervals at max | — | ~120 seconds |

**This is eventual consistency, not real-time mirroring.** The system guarantees that after any venue-side action on a market the wallet has ever touched, the deployment budget reflects it within approximately 30 seconds typically and 120 seconds under worst-case Data API lag. See `07_open_questions.md` OQ-6 for discussion of Data API propagation characteristics.

### Why NOT calling `_maintain_active_market` directly

The proposal suggested the Actor triggers `_maintain_active_market` directly. This is infeasible:

1. `_maintain_active_market` is a **private async method** on `PolymarketExecutionClient` (`execution.py:274`).
2. The Actor has no public reference to the exec client. It has `self.cache`, `self.portfolio`, `self.msgbus` — not the client instance.
3. Reaching into the kernel's exec engine to find the client is fragile and couples to internal structure.

Instead, we rely on the **existing continuous reconciliation loop** to open WS channels. When `generate_order_status_reports` processes orders for the newly cached instrument, it calls `_maintain_active_market` as a side effect (`execution.py:554`). This is architecturally clean: the Actor only adds instruments to Cache; the engine/adapter handle the rest through their existing machinery.

### Alternative: explicit reconciliation trigger (OPTIONAL, not required for correctness)

If the 20–45s latency is unacceptable, a future enhancement could have the Actor publish a `GenerateExecutionMassStatus` command on the message bus:

```python
self.msgbus.publish(
    topic=f"commands.trading.{client_id}",
    msg=GenerateExecutionMassStatus(...),
)
```

This triggers `reconcile_execution_state()` on the engine, which runs a full mass status + position reconciliation cycle. However:
- This is a heavy operation (all instruments, not just the new one).
- It may interfere with the ongoing continuous loop timing.
- For the stated objective (cap reopening within ~60s), the passive approach is sufficient.

This is noted as a future optimization, not a launch requirement.

## Shutdown

The `WalletSyncActor.on_stop()` cancels its timer and any in-flight executor tasks. The actor does not own any orders or positions — it is purely read-side. The existing shutdown drain logic (`guru_shutdown.py`, `shutdown_drain.py`) is unaffected.

### Shutdown and mid-cycle interruption

**`on_stop` behavior:** `on_stop` is a synchronous method (`cpdef void on_stop`, `actor.pxd:94`). The Nautilus framework does not await it. The implementation:

1. Cancel the timer: `self.clock.cancel_timer("wallet_sync")`. This prevents future cycles from being dispatched.
2. Cancel in-flight tasks: `self.cancel_all_tasks()` (`actor.pxd:150`). This requests cancellation of any sync cycle dispatched via `run_in_executor` (`actor.pxd:143`). The HTTP call inside the executor thread (`httpx.Client.get` in `fetch_wallet_position_rows`, `guru_cache_warmup.py:275`) is not directly cancellable by `asyncio` task cancellation, but the executor wrapper will not deliver the result after the task is cancelled, and the node shutdown proceeds regardless.

There is no need for an explicit drain-with-timeout because `on_stop` is synchronous — we cannot await anything. The `shutdown_cycle_drain_seconds` config key is reserved for a future enhancement if the framework gains async `on_stop` support, but is not used in the current implementation. The pattern is consistent with `GuruMonitorActor`, which also does not override `on_stop` at all.

**State written mid-cycle:** Instruments added to `Cache` via `force_add_instrument` before shutdown remain in a clean state. There is no partial-instrument corruption because `Cache.add_instrument` is atomic per-instrument — each call either fully adds the instrument or does not (`guru_instrument_dynamic.py:266–268`, `277–280`). The actor holds no transactional state across instruments — each instrument resolution is independent.

**Restart behavior:** The actor has **no persistent state of its own and no cleanup obligation on restart.** On next startup:

1. Compose-time warmup (`warm_polymarket_cache_from_wallet_positions`, `guru_cache_warmup.py:299–455`) reseeds Cache from current wallet positions.
2. The actor's first cycle re-diffs from scratch against a fresh `_known_condition_ids` set built from `Cache.instruments(venue=POLYMARKET)`.
3. Any instrument added in the interrupted run that is still wallet-relevant will simply be observed as already-cached.
4. Any instrument from the interrupted run that is no longer on the wallet is harmless — it remains in Cache but is not actively tracked.
5. The `_unresolvable_condition_ids` map starts empty on each process, so previously-exhausted retries are re-attempted on restart.

## Reconnect / WS disconnect

If the Polymarket user WS disconnects and reconnects:

1. The adapter handles WS reconnection internally (its own `_connect` / reconnect logic).
2. On reconnect, the adapter calls `_connect()` which iterates `self._cache.instruments(venue=POLYMARKET)` and calls `_maintain_active_market` for each → re-subscribes all cached instruments.
3. Because `WalletSyncActor` has been continuously maintaining Cache coverage, the reconnect picks up all markets — not just the ones present at initial connect time.
4. The WalletSyncActor continues polling independently of WS state. Even during a WS outage, it can still detect new wallet activity via HTTP and add instruments to Cache.

## Interaction with engine reconciliation

The WalletSyncActor and the engine's continuous reconciliation loop are **complementary, not conflicting**:

| Concern | WalletSyncActor | Engine continuous recon |
|---------|-----------------|----------------------|
| Discovers instruments | Yes (polls wallet) | No (cache-scoped) |
| Opens WS channels | No (indirect via recon) | Yes (via adapter reports) |
| Reconciles order state | No | Yes |
| Reconciles position state | No | Yes |
| Detects fills | No | Yes (via WS + reports) |
| Updates account state | No | Yes (adapter `_update_account_state`) |
| Updates Portfolio | No | Yes (engine processes events) |

The actor fills the one gap the engine cannot: discovering instruments that are not yet in Cache.
