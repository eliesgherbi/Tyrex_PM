# Path A Spike Findings: Synthetic Order Pre-Seeding

**Date:** 2026-04-15
**Status:** Hypothesis verified with critical modifications
**Confidence:** High (unit-level spike, deterministic)

## Executive Summary

The original hypothesis ("pre-seed an EXTERNAL/RECONCILIATION order before sending the PositionStatusReport") **does not work as designed** due to a position-ID collision in netting mode. However, a **modified approach** — constructing a synthetic order with the _original strategy's_ `strategy_id` and sending the fill directly through `ExecEngine.process` — is validated by the spike and cleanly closes the original position.

**Recommendation: Path A works with modifications. Proceed to implementation plan revision.**

---

## 1. Verified Failure Mechanism

### 1a. The engine's periodic check (`_check_positions_consistency`)

The engine's own periodic check at `live/execution_engine.py:970–1037` detects position discrepancies by comparing `Cache.positions_open()` against venue-reported positions. When it finds a mismatch, it:

1. Queries the adapter for real fills via `_query_and_find_missing_fills` (line 1028)
2. Receives `FillReport` objects with **real** `VenueOrderId`s from the Polymarket CLOB (the manual UI sells)
3. Calls `_reconcile_missing_fills` → `_reconcile_fill_report_single` for each fill (line 1225)

In `_reconcile_fill_report_single` (line 2124):
```
client_order_id = self._cache.client_order_id(report.venue_order_id)  # line 2136
```

This returns `None` because the VenueOrderIds from manual UI sells were never indexed in Nautilus cache. The method logs:
```
"FillReport received before OrderStatusReport for {venue_order_id}, deferring reconciliation"
```
and returns `False` (line 2144).

The deferral is **purely log-and-discard** — no retry queue. The next periodic check cycle re-discovers the same discrepancy and repeats. This is an infinite loop that never resolves because the orders that generated those fills will never appear in cache.

### 1b. The PositionStatusReport path (`_reconcile_position_report_netting`)

When our `WalletSyncActor` sends a `PositionStatusReport` via `msgbus.send("ExecEngine.reconcile_execution_report", report)`, the engine processes it through `_reconcile_position_report_netting` (line 2328). This method:

1. Detects the quantity mismatch between cache (29.66) and report (0)
2. Creates a synthetic `OrderStatusReport` with a random UUID4 `VenueOrderId` (line 2731)
3. Calls `_reconcile_order_report(diff_report, trades=[], is_external=False)` (line 2422)
4. Inside `_reconcile_order_report`:
   - Generates a new `ClientOrderId` (random UUID4)
   - Calls `_generate_order(report, is_external=False)` → creates an `EXTERNAL`/`RECONCILIATION` order (line 3334–3340)
   - Adds order to cache (line 2834)
   - Generates an `OrderAccepted` event (line 3034)
   - Calls `_handle_fill_quantity_mismatch` which generates an inferred fill via `create_inferred_order_filled_event` (line 2988)

**The inferred fill is created with** `position_id = PositionId(f"{instrument.id}-EXTERNAL")` (reconciliation.py, line 501) because `report.venue_position_id` is `None` and the fallback uses `-EXTERNAL`.

When this fill reaches `_handle_event` → `_determine_position_id` (engine.pyx, line 1357):
```
_determine_netting_position_id(fill)  → PositionId(f"{fill.instrument_id}-{fill.strategy_id}")
```

Since `fill.strategy_id = "EXTERNAL"`, the position ID becomes `f"{instrument_id}-EXTERNAL"` — which is **different** from the original position's ID (`f"{instrument_id}-{original_strategy}"`).

**Result:** The engine creates a **new** EXTERNAL SHORT position instead of closing the original. Two positions now exist:
- Original: LONG 29.66, strategy = original
- New: SHORT 29.66, strategy = EXTERNAL

The original position remains open. The deployment budget stays inflated.

### Root Cause (single sentence)

In netting mode, position IDs are `f"{instrument_id}-{strategy_id}"` (`engine.pyx:1452–1453`). Using `StrategyId("EXTERNAL")` for reconciliation creates a position-ID collision that prevents the fill from reaching the original position.

---

## 2. Synthetic Order Requirements

For a fill to correctly close the original position, it must have:

| Field | Required value | Reason |
|-------|---------------|--------|
| `strategy_id` | **Original position's strategy_id** (e.g., `CopyBotSellValidate-000`) | Netting position_id is `f"{instrument_id}-{strategy_id}"` |
| `client_order_id` | Any unique `ClientOrderId` | Must be indexed in cache for `_handle_event` to find the order |
| `venue_order_id` | Any synthetic `VenueOrderId` | Not used for position resolution; only for cache indexing |
| `order_side` | `OrderSide.SELL` | To reduce a LONG position |
| `quantity` | The delta (cache_qty - venue_qty) | Full or partial close |
| `position_id` on fill | `PositionId(f"{instrument_id}-{strategy_id}")` | Must match the existing position's ID |
| `reconciliation` | `True` | Tags the fill as reconciliation-origin |
| `tags` | `["RECONCILIATION"]` | Distinguishes from real fills in reporting |

**Cache pre-seeding required before sending the fill:**
1. `cache.add_order(order)` — so `_handle_event` can find it by `client_order_id`
2. `cache.update_order(order)` after applying `OrderAccepted` — indexes `venue_order_id`

**No `cache.add_position_id` pre-indexing needed** — `_determine_netting_position_id` computes the correct position_id from `fill.strategy_id`, which matches the original position when using the correct strategy.

### How to obtain the strategy_id

From the actor context:
```python
positions = self.cache.positions_open(instrument_id=iid)
if positions:
    strategy_id = positions[0].strategy_id
```

The position object exposes `strategy_id` as a public attribute (`Position.strategy_id` in the Cython model).

---

## 3. Data API Capability

The Polymarket Data API `/positions` endpoint returns:
- `conditionId`, `asset` (token_id), `size` (position quantity)
- **No `VenueOrderId`**, no trade details, no order information

The CLOB API `/data/trades` endpoint does return `VenueOrderId`s via the adapter's `generate_fill_reports` method. However, these VenueOrderIds belong to orders placed outside Nautilus (UI, other tools), so they can never be matched to cached orders.

**Implication:** The original hypothesis (pre-seeding an order with the real `VenueOrderId` for the engine's fill-matching to find) is **not feasible** — the Data API doesn't provide VenueOrderIds, and even if it did, the position-ID problem (Section 1b) would still prevent correct position closure.

The modified approach bypasses the engine's fill-matching entirely by constructing and sending the fill directly through `ExecEngine.process`. It requires no VenueOrderId from the Data API.

---

## 4. Spike Result

**The spike validates the modified approach.** Throwaway code in `spike_path_a.py`.

### Control test (demonstrates the bug)

Created a position with `strategy_id=CopyBotSellValidate-000`, then sent a fill with `strategy_id=EXTERNAL` via `ExecEngine.process`:

```
Open positions after EXTERNAL fill: 2
  id=...POLYMARKET-CopyBotSellValidate-000, qty=10.000000  (original, STILL OPEN)
  id=...POLYMARKET-EXTERNAL, qty=-10.000000                (new, unwanted)
Closed positions: 0
```

**Confirmed:** EXTERNAL strategy creates a duplicate position instead of closing the original.

### Main test (the fix)

Created a position with `strategy_id=CopyBotSellValidate-000`, then sent a fill with **the same** `strategy_id` via `ExecEngine.process`:

```
Open positions: 0
Closed positions: 1
  id=...POLYMARKET-CopyBotSellValidate-000, qty=0.000000, realized_pnl=0.000000
```

**Confirmed:** Using the original strategy's ID correctly closes the position through `ExecEngine.process`.

### What the spike does NOT test

- Thread safety of the `ExecEngine.process` call from `call_soon_threadsafe` (both run on the event-loop thread, so this should be identical to the current `msgbus.send` pattern)
- Interaction with the engine's periodic `_check_positions_consistency` (the periodic check would still log warnings for the external fills, but the position would already be closed by the time it runs)
- Strategy callback behavior (the original strategy will receive `on_order_filled` for the synthetic fill — needs handling)

---

## 5. Recommendation

**Path A works with the following modifications:**

### 5a. Change the injection mechanism

Replace:
```python
# Current: sends PositionStatusReport to reconciliation pipeline
self.msgbus.send("ExecEngine.reconcile_execution_report", position_status_report)
```

With:
```python
# New: construct order + fill and send through ExecEngine.process
order = self._create_synthetic_close_order(instrument, strategy_id, delta_qty)
self.cache.add_order(order)
# Apply accepted event
accepted = self._create_accepted_event(order)
order.apply(accepted)
self.cache.update_order(order)
# Create and send fill
fill = self._create_reconciliation_fill(order, instrument, delta_qty, position)
self.msgbus.send("ExecEngine.process", fill)
```

### 5b. Key design points for production implementation

1. **Strategy ID lookup:** Before building the synthetic order, look up the open position's `strategy_id` from cache. If no position exists (race: closed between diff computation and action application), skip the action.

2. **Position ID construction:** Use the netting convention `PositionId(f"{instrument_id}-{strategy_id}")` on the fill event. This matches what `_determine_netting_position_id` computes, but setting it explicitly avoids any ambiguity.

3. **Order lifecycle:** The synthetic order must go through INITIALIZED → ACCEPTED before the fill can be applied. The engine's `_handle_event` checks order status.

4. **Strategy callback:** The original strategy will receive `on_order_filled` for the reconciliation fill (the engine publishes to `events.order.{strategy_id}`). The strategy should check `fill.reconciliation == True` or `"RECONCILIATION" in fill.tags` and ignore it. This is a one-line guard in the strategy's event handler.

5. **Deployment budget recalculation:** After the position closes, the next deployment budget computation (which reads `Cache.positions_open()`) will see fewer open positions and compute a lower deployment. No changes needed to `NautilusDeploymentBudget`.

6. **Cleanup:** Residual synthetic orders (FILLED status) remain in cache. These are inert — they don't appear in `orders_open()` and don't affect deployment budget. No cleanup needed.

7. **`_check_positions_consistency` interaction:** The engine's own periodic check will still query the adapter for fills and log warnings for the unmatched venue_order_ids. However, since the position is now closed in cache, the discrepancy will not be detected on the next cycle. The warnings from the current cycle are harmless.

### 5c. Changes to the existing reconciliation plan

| Component | Change |
|-----------|--------|
| `WalletSyncActor._apply_reconciliation_actions` | Replace `msgbus.send("ExecEngine.reconcile_execution_report", PositionStatusReport)` with the new order + fill construction path |
| `WalletSyncActor._reconciliation_pass` | Add position strategy_id lookup during diff computation |
| `ReconciliationAction` dataclass | Replace `report: PositionStatusReport | None` with the synthetic order+fill objects, or keep it simple and just carry the strategy_id |
| Strategy (`CopyStrategy` / `BotSellValidateStrategy`) | Add one-line guard: `if fill.reconciliation: return` in `on_order_filled` |
| Plan docs (01_design.md, 02_components.md) | Update injection mechanism from PositionStatusReport to ExecEngine.process |
| Plan docs (00_overview.md Alternatives table) | Move PositionStatusReport from "Chosen" to "Rejected" with the position-ID collision reason; add ExecEngine.process as new "Chosen" |

### 5d. New edge cases

1. **Multiple strategies per instrument:** If multiple strategies have open positions for the same instrument, the diff should be per-position, not per-instrument. The venue truth is a single quantity; the actor needs to decide which strategy's position to close. Simplest policy: close positions in FIFO order (oldest first).

2. **Partial close with wrong strategy:** If the venue shows partial reduction and the actor picks the wrong strategy to close, it would reduce the wrong position. This is mitigated by the fact that Polymarket positions are typically managed by a single strategy per instrument.

3. **Race: position closed between diff and application:** The position might be closed by a real fill between the time the diff is computed (executor thread) and the time the action is applied (event-loop thread). Guard: check `cache.positions_open(instrument_id=iid)` in `_apply_reconciliation_actions` before constructing the order. If empty, skip.

---

## 6. Time and Confidence

**Time spent:** ~3 hours of investigation and spike development.

**Confidence level:** **High** for the core finding and the spike result. The spike uses the real Nautilus `ExecutionEngine`, `Cache`, and `MessageBus` — no mocks for the critical path. The position is genuinely closed in the cache.

**Remaining uncertainty:**
- Strategy callback handling needs verification in the live strategy (one-line guard, but must be tested)
- Interaction with the engine's periodic consistency check in a live environment (should be benign, but unverified)
- The `OrderSide.SELL` position_side display shows as `2` in the spike output (enum integer) — cosmetic, not functional

---

## Appendix: Code References

| Code location | Line(s) | What it shows |
|---------------|---------|---------------|
| `execution_engine.py` `_reconcile_fill_report_single` | 2136–2144 | Deferral: `cache.client_order_id(venue_order_id)` returns None |
| `execution_engine.py` `_reconcile_position_report_netting` | 2328–2461 | PositionStatusReport processing path |
| `execution_engine.py` `_create_position_reconciliation_report` | 2648–2744 | Synthetic OrderStatusReport with UUID4 venue_order_id |
| `execution_engine.py` `_reconcile_order_report` | 2797–2870 | Order generation + inferred fill path |
| `execution_engine.py` `_generate_order` | 3286–3391 | EXTERNAL strategy assignment when `is_external=False` |
| `reconciliation.py` `create_inferred_order_filled_event` | 427–514 | Fill with `position_id=f"{instrument.id}-EXTERNAL"` fallback (line 501) |
| `engine.pyx` `_determine_netting_position_id` | 1452–1453 | `PositionId(f"{fill.instrument_id}-{fill.strategy_id}")` |
| `engine.pyx` `_handle_event` | 1167–1278 | Main event processing: determines position_id, applies fill |
| `engine.pyx` `_handle_position_update` | 1570–1578 | Position lookup by `fill.position_id`, update or create |
| `engine.pyx` base `process` | 823–835 | Public entry point: `self._handle_event(event)` |
| `engine.pyx` msgbus registration | 188 | `ExecEngine.process` is a registered endpoint |
| `polymarket/execution.py` `generate_fill_reports` | 607–657 | Adapter returns FillReports with real VenueOrderIds |
| `polymarket/execution.py` `_parse_trades_response_object` | 703–753 | VenueOrderId extraction from CLOB trades |
| `cache.pyx` `add_order` | 1949–2010 | Does NOT index venue_order_id |
| `cache.pyx` `add_venue_order_id` | 1903–1945 | Explicit venue_order_id → client_order_id indexing |
| `spike_path_a.py` | — | Throwaway spike code validating the approach |
