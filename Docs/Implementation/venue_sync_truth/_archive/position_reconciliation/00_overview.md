# Position Reconciliation from Wallet Truth — Overview

## Problem

`WalletSyncActor` discovers instruments from the Data API `/positions` payload but does
not reconcile *position quantities*. When a position is closed or reduced externally
(Polymarket UI, another bot, manual API call), Nautilus's `Cache.positions_open()` retains
the stale entry because it only tracks positions originating from its own order events.

`NautilusDeploymentBudget.filled_polymarket_usd()` reads `Cache.positions_open()` (line 149,
`deployment_budget.py`). Stale positions inflate the filled leg → portfolio cap blocks new
BUY orders indefinitely, even though the wallet is empty on the venue.

## Proposal

Extend `WalletSyncActor` with a **position-reconciliation pass** that runs after instrument
discovery on each poll cycle. It:

1. Builds a venue-truth position map from the Data API `/positions` payload the actor
   already fetches.
2. Diffs venue-truth against `Cache.positions_open()`.
3. When an external close or reduction is detected, sends a `PositionStatusReport` to
   `ExecEngine.reconcile_execution_report` via the MessageBus — triggering the framework's
   own netting reconciliation pipeline to synthesize EXTERNAL/RECONCILIATION orders and fills.

**No Nautilus code is modified.** The actor uses the same public MessageBus endpoint that the
engine's periodic position-check cycle and the `PolymarketExecutionClient._send_fill_report`
path both use. The rollout is **shadow-mode-first**: the diff algorithm and fact emission
are deployed in observation-only mode before engine-state mutation is enabled (see
`06_migration.md` Step 7 and `05_lifecycle.md` §Shadow mode).

## Single-truth philosophy

The deployment budget continues to read only `Cache.positions_open()`. The cache remains
the single truth. The actor keeps it honest from outside by injecting reports through
Nautilus's normal event-application path. No parallel position store, no fallback paths.

## Injection mechanism (verified)

The chosen injection path is:

```
Actor.msgbus.send("ExecEngine.reconcile_execution_report", PositionStatusReport)
```

**Framework path (nautilus_trader 1.222.0):**

1. `LiveExecutionEngine` registers endpoint `"ExecEngine.reconcile_execution_report"` →
   `self.reconcile_execution_report` (`live/execution_engine.py:249–252`).
2. `reconcile_execution_report` dispatches `PositionStatusReport` to
   `_reconcile_position_report` (line 1798–1799).
3. Polymarket uses netting OMS (no `venue_position_id`), so
   `_reconcile_position_report_netting` is called (line 2274).
4. Netting reconciliation compares `sum(p.signed_decimal_qty())` for
   `Cache.positions_open(instrument_id=...)` against `report.signed_decimal_qty`.
5. On mismatch (and `generate_missing_orders=True`, default per `live/config.py:201`),
   the engine calls `_create_position_reconciliation_report` (line 2414–2422) which
   builds a synthetic `OrderStatusReport` with side/qty/price.
6. `_reconcile_order_report` (line 2797+) creates a synthetic order via `_generate_order`
   (strategy_id=`StrategyId("EXTERNAL")`, tags=`["RECONCILIATION"]`, `reconciliation=True`)
   and adds it to cache.
7. The order is filled via `_handle_fill_quantity_mismatch` → `_generate_inferred_fill` →
   `create_inferred_order_filled_event` (`live/reconciliation.py:427–514`).
8. `OrderFilled` flows through `_handle_event_with_tracking` → `_handle_event` →
   `_apply_event_to_order` (cache update) → `_handle_order_fill` (position update,
   portfolio update, fill topic publication).

**Result:** The stale position in cache is closed. `positions_open()` drops it.
`NautilusDeploymentBudget.filled_polymarket_usd()` sees the lower total. Portfolio cap
unblocks.

## Alternatives evaluated and rejected

| Path | Verdict |
|------|---------|
| Direct `Cache` mutation (`cache.update_position`, `cache.delete_position`) | **Rejected.** Bypasses Portfolio, MessageBus event publication, order tracking. Breaks the execution event chain that downstream consumers (reporting, strategy `on_order_filled`) rely on. |
| `msgbus.send("ExecEngine.process", synthetic_OrderFilled)` | **Rejected.** Requires a pre-existing `Order` in cache (engine's `_handle_event` loads order from cache by `client_order_id`; returns early with error if not found). Would need manual order creation + cache insertion, duplicating engine internals. |
| `FillReport` via `msgbus.send("ExecEngine.reconcile_execution_report", fill_report)` | **Rejected.** `_reconcile_fill_report_single` requires `client_order_id` already indexed from `venue_order_id` in cache (line 2130); returns `False` if not found. Would need synthetic order pre-seeded. Two-step process with more race surface than `PositionStatusReport`. |
| `PositionStatusReport` via `msgbus.send(...)` | **Chosen.** Single-step: engine handles diffing, order creation, fill synthesis, cache update, portfolio update end-to-end. Actor only needs to state "venue says instrument X has quantity Y." Exactly what the engine's own periodic `_check_positions_consistency` does. |

## Correctness guarantee

The reconciliation pass is bounded by the `WalletSyncActor` poll interval (default 15s).
Under normal conditions, an external close is reflected in cache within one poll cycle
(~15–30s including Data API latency). The p99 case (Data API lag + deferred due to in-flight
order) is bounded by `position_reconciliation_deferral_max` cycles (default 5 × 15s = 75s).

## Known accuracy trade-offs

Any position closed outside Tyrex (Polymarket UI, another strategy, manual API call)
will have approximate realized PnL in cache and downstream reports, because the actual
close price is unknown to the system. These closes are distinguishable by
`strategy_id == "EXTERNAL"`, `tags == ["RECONCILIATION"]`, and
`OrderFilled.reconciliation == True`. Reporting consumers that aggregate realized PnL
should either exclude reconciliation-origin fills, mark them as approximate, or document
the limitation in their output. See `01_design.md` §PnL accounting for details.

## Explicit non-goals

- **No upstream Nautilus changes.** Framework is in active development; upstream contributions
  revisited after future releases.
- **No parallel position store.** No `WalletPositionTruth`, no reconciliation-specific caches.
- **No Polymarket UI as source of truth.** Only the Data API `/positions` endpoint.
- **No modification of the deployment budget's read path.** `NautilusDeploymentBudget`
  continues to read `Cache.positions_open()` exactly as today.
- **No new abstractions.** No `PositionReconciler` class. The actor owns the wallet truth
  fetch; it owns the reconciliation that uses it.
