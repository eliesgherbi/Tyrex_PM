# Position Reconciliation — Design

## Diff algorithm

### Step 1: Build venue-truth position map

The `_sync_cycle` method already fetches `/positions` rows. Each row contains
`conditionId` (or `condition_id`), `asset` (or `token_id`), and crucially the
`size` field (string decimal of the position quantity on the venue).

The reconciliation pass builds:

```python
VenuePositionMap = dict[InstrumentId, Decimal]
```

For each row in the positions payload:

1. Extract `token_id` (the `asset` field) and `size`.
2. Resolve `token_id` → `InstrumentId` via `Cache.instruments(venue=POLYMARKET_VENUE)` scan
   (same pattern as `instrument_id_for_outcome_token` in `state_readers.py:52–66`).
   If the instrument is not in cache, skip (the discovery pass handles adding it).
3. Parse `size` to `Decimal`. If zero or negative, record as `Decimal(0)`.
4. Aggregate by `InstrumentId` (a condition may have multiple outcomes; each outcome token
   has its own `InstrumentId`).

### Step 2: Build cache position map

```python
CachePositionMap = dict[InstrumentId, Decimal]
```

For each position in `self.cache.positions_open(venue=POLYMARKET_VENUE)`:

1. Key: `position.instrument_id`.
2. Value: `position.signed_decimal_qty()` (same accessor used by
   `_reconcile_position_report_netting` at `live/execution_engine.py:2350–2353`).

### Step 3: Diff

For every `InstrumentId` in the **union** of venue-truth and cache maps:

| Case | venue_qty | cache_qty | Action |
|------|-----------|-----------|--------|
| **Match** | V | C where V == C | No-op. |
| **Stale close** | 0 (or absent) | C > 0 | Emit `PositionStatusReport` with `quantity=0`, `position_side=FLAT`. |
| **Stale partial** | V > 0, V < C | C > V | Emit `PositionStatusReport` with `quantity=V`, `position_side=LONG`. |
| **Venue-has-more** | V > C | C (including C=0) | **No-op / defer.** See Race B defense. |

In the stale-close and stale-partial cases, the report triggers the engine's netting
reconciliation which handles all synthetic order creation, fill injection, cache update,
and portfolio update.

### Step 4: Inject via MessageBus

For each report:

```python
self.msgbus.send("ExecEngine.reconcile_execution_report", report)
```

This is a synchronous call (MessageBus `send` calls the handler inline —
`component.pyx:2536–2558`). The entire reconciliation pipeline executes before
`send` returns. No async gap, no event-loop yield.

**Thread safety note:** `_sync_cycle` runs in an executor thread via `run_in_executor`.
`msgbus.send` is **not** documented as thread-safe. The `PositionStatusReport` must be
sent from the event-loop thread. See §Lifecycle for the callback-based design.

## Synthetic event shape

The actor does **not** construct synthetic `OrderFilled` events directly. It constructs
only `PositionStatusReport` objects. The engine handles all downstream event synthesis.

### What the engine creates (documented for reporting/observability)

When the engine processes the `PositionStatusReport` and finds a qty mismatch:

1. **`OrderStatusReport`** — synthetic, created by `_create_position_reconciliation_report`
   (`live/execution_engine.py:2648–2711`):
   - `order_side`: `SELL` when cache > venue, `BUY` when cache < venue.
   - `quantity`: `abs(cache_qty - venue_qty)`, made precise via `instrument.size_precision`.
   - `price`: calculated by `calculate_reconciliation_price()` using position avg_px_open
     and report avg_px_open. Falls back to last quote or position avg_px.
   - `order_status`: `FILLED`.
   - `filled_qty`: same as `quantity`.

2. **`OrderInitialized`** — created by `_generate_order` (`live/execution_engine.py:3360–3386`):
   - `strategy_id`: `StrategyId("EXTERNAL")`.
   - `tags`: `["RECONCILIATION"]`.
   - `reconciliation`: `True`.
   - `client_order_id`: `ClientOrderId(UUID4().value)` (generated in
     `_resolve_client_order_id`, line 2901).

3. **`OrderFilled`** — created by `create_inferred_order_filled_event`
   (`live/reconciliation.py:427–514`):
   - `strategy_id`: `StrategyId("EXTERNAL")`.
   - `trade_id`: `TradeId(UUID4().value)` (synthetic).
   - `position_id`: `report.venue_position_id or PositionId(f"{instrument.id}-EXTERNAL")`.
   - `reconciliation`: `True`.
   - `last_qty`: diff quantity.
   - `last_px`: reconciliation price or inferred price.

### PnL accounting for synthetic closes

A position closed by a synthetic `RECONCILIATION` fill records PnL based on the
reconciliation price (which defaults to the position's `avg_px_open` when no quote is
available — effectively zero realized PnL for a close at entry price). This is
intentionally conservative: the real execution price is unknown to Tyrex (the close
happened externally), so the reported PnL for reconciliation-origin closes should be
treated as approximate. See `00_overview.md` §Known accuracy trade-offs for the
operator-facing impact of this limitation.

### Distinguishing reconciliation-origin closes from real closes

Reporting consumers can identify reconciliation-origin closes by:

1. **`strategy_id == "EXTERNAL"`** on the order/fill.
2. **`tags` contains `"RECONCILIATION"`** (as opposed to `"VENUE"` for venue-discovered
   external orders).
3. **`reconciliation == True`** on the `OrderFilled` event.

The `position_reconciliation` fact (new) will log every reconciliation action with
the instrument, diff direction, and quantities for audit.

## Venue-has-more case

When venue reports a position quantity **greater** than cache (or cache has no position
but venue does), this means the position was opened externally. The framework's existing
startup reconciliation and periodic `_check_positions_consistency` cycle already handle
this case by querying fill reports from the adapter. The actor does **not** attempt to
synthesize opening fills — only the engine's fill-report-based reconciliation (which
queries the adapter's `generate_fill_reports`) can correctly attribute the entry price.

However, sending a `PositionStatusReport` with the venue quantity will trigger the engine
to detect the discrepancy and run its fill-query reconciliation. This is safe: the engine
already guards against duplicate fills via trade_id deduplication. The actor can
optionally send the report in the venue-has-more case to accelerate the engine's awareness,
gated by config (`reconcile_venue_has_more`, default `false`).

## Idempotence

Multiple cycles reporting the same venue state produce no new effects:

- If cache already matches venue, `_reconcile_position_report_netting` sees
  `quantities_match == True` and returns (line 2359).
- If a previous cycle already injected the reconciliation fill, cache now matches
  venue; same code path, no duplicate action.
- The engine's `_find_matching_cached_order` check (`live/execution_engine.py:2703`)
  prevents duplicate synthetic orders even if called concurrently.
