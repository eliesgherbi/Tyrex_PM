# 01 — Target Architecture

## Design principle

Close the adapter's cache-scope gap **at the input** (instrument discovery), not at the output (parallel truth store). Once an instrument is in `Cache`, the adapter's WS handling, report generation, and the engine's reconciliation all work correctly. The fix is therefore: **make sure every market the wallet touches is in `Cache` continuously, not just at startup.**

## Architecture diagram (plain language)

```
┌──────────────────────────────────────────────────────────┐
│                     TradingNode                          │
│                                                          │
│  ┌─────────────────────┐   ┌────────────────────────┐   │
│  │  WalletSyncActor    │   │  PolymarketExecClient   │   │
│  │  (NEW — Tyrex Actor) │   │  (installed adapter)    │   │
│  │                     │   │                          │   │
│  │ • polls py-clob     │   │ • WS user stream        │   │
│  │   get_orders()      │   │ • order/fill/position    │   │
│  │   + Data API        │   │   reports (cache-scoped) │   │
│  │   /positions        │   │ • _maintain_active_market│   │
│  │ • diffs vs Cache    │   │ • _update_account_state  │   │
│  │ • resolves missing  │   │                          │   │
│  │   instruments →     │   └──────────┬───────────────┘   │
│  │   Cache.add_instr.  │              │                   │
│  │ • emits facts       │              │ generates         │
│  └──────────┬──────────┘              │ reports           │
│             │ adds instruments        │                   │
│             ▼                         ▼                   │
│  ┌─────────────────────────────────────────────────────┐ │
│  │                    Cache                             │ │
│  │  instruments / orders / positions / account          │ │
│  └─────────────────────┬───────────────────────────────┘ │
│                        │                                  │
│  ┌─────────────────────▼───────────────────────────────┐ │
│  │              LiveExecutionEngine                     │ │
│  │  • startup mass status reconciliation               │ │
│  │  • continuous open-order check (open_check_*)       │ │
│  │  • continuous position check (position_check_*)     │ │
│  │  → all cache-scoped, now correct because            │ │
│  │    WalletSyncActor ensures coverage                 │ │
│  └─────────────────────┬───────────────────────────────┘ │
│                        │                                  │
│  ┌─────────────────────▼───────────────────────────────┐ │
│  │               Portfolio                              │ │
│  │  account(venue) / positions / is_flat                │ │
│  └─────────────────────┬───────────────────────────────┘ │
└────────────────────────┼──────────────────────────────────┘
                         │
         ┌───────────────▼────────────────────┐
         │  Tyrex risk / deployment / capital  │
         │                                     │
         │  NautilusDeploymentBudget           │
         │    .pending_polymarket_usd()        │
         │    .filled_polymarket_usd()         │
         │    (reads Cache.orders_open,        │
         │     Cache.positions_open)           │
         │                                     │
         │  NautilusAccountSnapshotProvider    │
         │    .snapshot()                       │
         │    (reads Portfolio.account)         │
         │                                     │
         │  ConfiguredRiskPolicy               │
         │    .evaluate()                       │
         └─────────────────────────────────────┘
```

## Ownership boundaries (target state)

| Component | Owns | Reads |
|-----------|------|-------|
| **WalletSyncActor** | Instrument discovery; ensures cache coverage | py-clob orders, Data API positions, `Cache` instrument set |
| **PolymarketExecClient** (adapter) | WS subscriptions, order/fill/position reports, account state | `Cache` instruments (scope unchanged) |
| **LiveExecutionEngine** | Reconciliation, applying reports to Cache/Portfolio | `Cache`, adapter reports |
| **Cache** | Instruments, orders, positions | — |
| **Portfolio** | Account state, position accounting | `Cache` |
| **NautilusDeploymentBudget** | Deployment cap math | `Cache.orders_open`, `Cache.positions_open`, `Portfolio.is_flat` |
| **NautilusAccountSnapshotProvider** | — | `Portfolio.account(venue)` |
| **ClobAllowanceStateProvider** | — | py-clob `get_balance_allowance` |
| **ConfiguredRiskPolicy** | Pre-trade gate decisions | Deployment budget, capital provider |

## Data flow for key scenarios

### Bot places order on known market
Same as today. No change.

### Human buys on never-loaded market via Polymarket UI

1. **WalletSyncActor** polls `get_orders()` / Data API `/positions` → sees new `condition_id` not in `Cache`.
2. Actor resolves instrument via Gamma/CLOB → `parse_polymarket_instrument` → `Cache.add_instrument`.
3. On next adapter reconciliation cycle (`open_check_interval_secs` / `position_check_interval_secs`), adapter now sees the instrument → generates reports → engine reconciles → `Cache` and `Portfolio` updated.
4. **Additionally**, the actor submits a targeted `GenerateOrderStatusReport` command to the exec engine for the new instrument. This internally calls `_maintain_active_market` on the exec client (`execution.py:554`), opening the WS channel. This accelerates state catch-up vs waiting for the next periodic check.
5. `NautilusDeploymentBudget.filled_polymarket_usd()` reads `Cache.positions_open` → includes the new position.

### Cap hit → human exits on venue → cap reopens

1. Human sells/cancels on Polymarket UI.
2. Instrument is **already in Cache** (was loaded when position was opened).
3. Adapter user-WS delivers `TRADE` / `CANCELLATION` events → `Cache` and `Portfolio` update.
4. Or: periodic reconciliation detects the change.
5. `NautilusDeploymentBudget.pending_polymarket_usd()` / `filled_polymarket_usd()` reflect the freed exposure.
6. Next `ConfiguredRiskPolicy.evaluate()` → lower `portfolio_deploy_at_eval` → cap no longer binds.

### Startup with pre-existing wallet state

1. Compose-time wallet warmup (`warm_polymarket_cache_from_wallet_positions`) runs as today, seeding `Cache`.
2. **WalletSyncActor** starts on `on_start`. Immediately runs a full sync cycle (poll → diff → resolve).
3. Any markets missed by warmup (resolution failures, cap limits) get a second chance.
4. Actor blocks readiness gate until first sync completes (see `04_lifecycle.md`).
5. Startup reconciliation runs → all cached instruments get order/fill/position reports.

## What this does NOT fix (see `07_open_questions.md`)

- **Position reports remain cache-scoped at the adapter level.** The adapter's `generate_position_status_reports` iterates `self._cache.instruments(venue=POLYMARKET)`. Even with `use_data_api=true`, it maps Data API rows back to the cached instrument set (`execution.py:788–791`). The **engine's** `_process_venue_reported_positions` (`execution_engine.py:1101–1153`) can detect venue-reported positions for instruments not in its `positions_by_instrument` dict — but only if the **adapter** returns them. Until the adapter emits position reports for un-cached instruments, there is a window between "instrument added to cache" and "position reconciled" that depends on the next reconciliation cycle.

- **The order status report `get_orders()` API on the adapter is `open_only` by default.** The adapter calls `self._http_client.get_orders(params)` which returns only **active** orders. Historical / filled / canceled orders for newly-discovered markets are not directly recovered — they come through fills and position reconciliation. This is consistent with how Nautilus handles external orders (creates them as `EXTERNAL` during reconciliation).
