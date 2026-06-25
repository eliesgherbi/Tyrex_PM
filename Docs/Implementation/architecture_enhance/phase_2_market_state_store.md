# Phase 2 — MarketStateStore

**Program:** [README.md](README.md) · **Prev:** [phase_1_generic_signal_strategy_dispatch.md](phase_1_generic_signal_strategy_dispatch.md) · **Next:** [phase_3_execution_planner.md](phase_3_execution_planner.md)

> **Procedural now, event-ready later.** A store with a clean update method; no bus.

---

## 1. Goal

Make market data a **shared, stale-aware state component** instead of ad-hoc REST calls scattered through strategy/test pricing helpers. Deliver a real `MarketStateStore` with a minimum query interface:

```python
MarketStateStore.best_bid(token_id)        -> Decimal | None
MarketStateStore.best_ask(token_id)        -> Decimal | None
MarketStateStore.spread(token_id)          -> Decimal | None
MarketStateStore.mid(token_id)             -> Decimal | None
MarketStateStore.last_update_ts(token_id)  -> datetime | None
MarketStateStore.is_stale(token_id, max_age_s=...) -> bool
MarketStateStore.estimate_fill_price(token_id, side, size) -> Decimal | None
MarketStateStore.estimate_slippage(token_id, side, size)   -> Decimal | None
```

This store will feed `ExecutionPlanner` (Phase 3), `ProtectionEngine` (Phase 4), and risk marks.

---

## 2. Why this phase exists

The review found `state/market_store.py` and `ingestion/market_stream.py` are **scaffolds** (`"""Market stream supervisor — Phase 10."""` with no body). Book reads happen ad hoc in `strategies/sell_test/pricing.py` (`fetch_order_book`, `best_levels_from_book`) and are duplicated by `tp_sl_test`. There is no single source for best bid/ask, no staleness detection, and no slippage estimator.

Without this, the planner (Phase 3) and production TP/SL (Phase 4) would each re-invent book access — exactly the duplication this program exists to remove.

---

## 3. Current state

```text
state/market_store.py        → MarketStateStore referenced in docs; near-empty scaffold
ingestion/market_stream.py   → "Market stream supervisor — Phase 10." (no implementation)
venue/polymarket/market_ws.py→ market WS adapter (scaffolded; not consumed by strategies)
strategies/sell_test/pricing.py → fetch_order_book / best_levels_from_book (REST, ad hoc)
strategies/tp_sl_test/strategy.py → uses sell_test pricing for SELL price at trigger
runtime/coordinator.py       → marks_for_risk() builds marks from positions; no book source
```

`state/market_store.py` is described in `Docs/modules/state/README.md` as "Last-known mid/last for tokens" — i.e. intended but minimal.

---

## 4. Target state

```text
state/market_store.py
  MarketBookSnapshot   # frozen: token_id, best_bid, best_ask, bids[], asks[], ts_utc, source
  MarketStateStore
    apply_snapshot(snapshot)            # single writer: ingestion/market_stream
    apply_book_delta(token_id, delta)   # incremental update path
    best_bid / best_ask / spread / mid / last_update_ts / is_stale
    estimate_fill_price(token_id, side, size)
    estimate_slippage(token_id, side, size)

ingestion/market_stream.py
  run_market_ws_ingest(...)   # subscribe to token set; apply_snapshot/apply_book_delta
                              # single-writer per the state/ ownership rule

venue/polymarket/book_snapshot.py    (or extend an existing REST client)
  fetch_book_snapshot(token_id)       # REST bootstrap / repair when WS cold

runtime/coordinator.py
  market_state: MarketStateStore | None   # exposed to risk/planner/protection
  marks_for_risk()                          # may prefer MarketStateStore.mid when fresh
```

`StrategyContext.market_state` (reserved in Phase 1) is now populated from `coord.market_state`.

---

## 5. Non-goals

- No event bus; `market_stream` writes the store directly (single-writer per `state/` rules).
- No execution planner yet (Phase 3 consumes this store).
- No protection engine yet (Phase 4 consumes this store).
- No deletion of `sell_test`/`tp_sl_test` pricing helpers **in this phase** — they are replaced once the planner/protection consume the store (Phase 3/4). Document the deprecation; do not rip out yet.
- No depth analytics beyond a simple walk-the-book fill estimate.

---

## 6. Files likely to change

```text
src/tyrex_pm/state/market_store.py            # implement MarketStateStore + snapshot model
src/tyrex_pm/ingestion/market_stream.py       # implement WS ingest → store writer
src/tyrex_pm/venue/polymarket/market_ws.py    # finalize market WS subscribe/parse if needed
src/tyrex_pm/runtime/coordinator.py           # hold + expose market_state; marks via store
src/tyrex_pm/runtime/app.py                   # start market_stream supervisor when enabled
src/tyrex_pm/runtime/live_supervisor.py       # (if REST book repair runs on the refresh loop)
Docs/modules/state/README.md                  # document the real MarketStateStore
Docs/modules/ingestion/README.md              # document market_stream
Docs/LIVE_ARCHITECTURE.md                      # market-data spine + staleness semantics
```

---

## 7. New files likely to be added

```text
src/tyrex_pm/venue/polymarket/book_snapshot.py   # REST book bootstrap/repair (if not folded elsewhere)
tests/test_market_state_store.py
tests/test_market_stream_ingest.py
```

---

## 8. Data model changes

In `state/market_store.py` (not `core/models.py`, to keep `core` venue-agnostic; promote later if shared):

```python
@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal

@dataclass(frozen=True)
class MarketBookSnapshot:
    token_id: TokenId
    best_bid: Decimal | None
    best_ask: Decimal | None
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    ts_utc: datetime
    source: str           # "market_ws" | "rest"
```

`MarketStateStore` holds `dict[TokenId, MarketBookSnapshot]` plus per-token `last_update_ts`. **Decimal everywhere**; use `core/time.utc_now`.

---

## 9. Config changes

```yaml
# config/runtime/default.yaml (new block)
market_data:
  enabled: false                 # off by default until consumers exist
  ws_subscribe_tokens: []        # explicit token ids, or derived from strategy/protection
  stale_after_s: 5.0             # is_stale threshold
  rest_bootstrap: true           # seed book via REST before WS warms up
  rest_repair_interval_s: 30.0   # periodic REST reconcile of book (optional)
```

`runtime/config.py`: typed `MarketDataConfig`. Default **disabled** so Phase 2 lands without changing live behavior until Phase 3 turns it on.

### Dark-launch and activation boundary (locked)

Phase 2 ships **dark**: `market_data.enabled=false`, no `market_stream` supervisor started, no consumer. The store is fully implemented and unit-tested in isolation, but nothing in the live path reads it yet.

**Phase 3 owns the activation contract.** When the `ExecutionPlanner` is enabled, market data must be enabled too, and Phase 3 defines exactly when fresh book data is *required* vs *optional*:

| Plan path | Market data requirement |
|-----------|--------------------------|
| Passive / normal **entry** | Optional — planner can use the strategy limit price if the book is stale/missing. |
| Urgent / protection **exit** | **Required** — stale or missing book ⇒ deny / fail closed unless an explicit safe fallback is configured. |

This avoids the confusing state where the planner is on but market data is off. The cross-check ("planner enabled ⇒ market data enabled") is enforced at config load in Phase 3, not Phase 2.

---

## 10. Fact/reporting changes

Market ticks are **high-frequency**; do **not** emit a fact per book update. Options:

- No new fact in Phase 2 (preferred). Staleness/availability becomes visible indirectly via planner/protection facts (Phase 3/4) and `health`.
- Optionally extend `health` (`FACT_TYPE_HEALTH`) with a `market_data_ok` / `stale_token_count` field, deduped like `wallet_sync`.

**Decision:** no per-tick facts; optional health enrichment only, dedup-guarded.

---

## 11. Tests to add

```text
tests/test_market_state_store.py
  test_market_store_best_bid_ask_update
  test_market_store_spread_and_mid
  test_market_store_stale_detection
  test_estimate_fill_price_walks_book
  test_estimate_slippage_thin_book
  test_missing_token_returns_none_not_error

tests/test_market_stream_ingest.py
  test_market_stream_applies_book_delta
  test_market_stream_single_writer_only
  test_rest_snapshot_bootstraps_market_store
```

---

## 12. Acceptance criteria

- `MarketStateStore` can be populated in tests and answers best bid/ask/spread/mid for a token.
- Stale book detection works (`is_stale` true past `stale_after_s`).
- REST fallback can seed the book before WS updates.
- `estimate_fill_price` / `estimate_slippage` walk available depth and return `None` (not crash) on empty book.
- No strategy directly owns book state; `StrategyContext.market_state` is the access path.
- Default config keeps market data disabled (dark launch); live behavior is unchanged until Phase 3 activates consumers.
- The store's read API (`is_stale`, `estimate_fill_price`) is sufficient for Phase 3's required-fresh-book rule for urgent exits.

---

## 13. Migration risks

- **WS parsing correctness:** Polymarket market channel sends snapshots + price changes; mis-parsing yields wrong bid/ask. Mitigate with fixture-driven `test_market_stream_applies_book_delta`.
- **Staleness false positives** under normal quiet markets. Make `stale_after_s` configurable; default conservatively.
- **Single-writer violation:** both WS and REST repair writing the store. Enforce one writer (WS primary; REST repair routed through the same store method with a `source` tag and last-writer-wins by `ts_utc`).
- **Performance:** per-tick allocations. Keep snapshots immutable but cheap; avoid emitting facts.

---

## 14. Rollback strategy

- Feature-flagged by `market_data.enabled=false`. Disabling the flag reverts to the pre-Phase-2 runtime (no market_stream supervisor started).
- The store is additive; nothing else depends on it until Phase 3. Rollback = disable flag or revert the commit; no impact on risk/OMS/reconcile.

---

## 15. Dependencies on previous phases

- **Phase 1** provides `StrategyContext.market_state` slot to populate.
- **Phase 0** documents the market-data spine.

Provides for: **Phase 3** (planner book inputs), **Phase 4** (protection triggers + exit pricing), and improved risk marks.

---

## 16. Event-ready design notes

`MarketStateStore.apply_snapshot(snapshot)` / `apply_book_delta(...)` are the single mutation surface. Today `ingestion/market_stream` calls them in its loop; a future event bus could route `MarketBookSnapshot` events to the same methods with no change. Read methods (`best_bid`, `is_stale`, `estimate_*`) are pure queries safe for any caller. No bus, no subscriptions added.
