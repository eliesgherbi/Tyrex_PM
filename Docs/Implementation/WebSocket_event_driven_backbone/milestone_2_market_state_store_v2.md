# Milestone 2 — MarketStateStore v2

## Objective

Extend `MarketStateStore` with **source metadata**, **event-time fields**, **immutable capture APIs**, and **event ordering state** while preserving **backward-compatible** existing accessors.

## Why this milestone exists

Current `MarketBookSnapshot.ts` is wall-clock at apply time with no source. Shadow WS (M1) and WS-authoritative mode (M8) need `received_ts`, `source`, `snapshot_id`, and sequence tracking for gap detection.

## Current codebase state

**Confirmed:** `state/market_store.py` — `apply_snapshot`, `estimate_fill_price`, `is_stale`. Single writer convention.

## Target behavior

```python
# Backward compatible
store.snapshot(token_id)
store.is_stale(token_id, max_age_s=...)
store.estimate_fill_price(token_id, side, size)

# New v2 APIs
store.capture(token_id) -> MarketStateSnapshot | None
store.capture_pair(yes, no, pair_id) -> PairMarketSnapshot | None
store.book_age_ms(token_id) -> int | None
store.apply_snapshot(..., source=BookSource, exchange_ts=None, sequence=None)

# Event ordering per token (authoritative store — used from M8)
store.last_sequence(token_id) -> int | None
store.reconnect_gap(token_id) -> bool
store.set_reconnect_gap(token_id, value: bool) -> None
```

**M1 note:** Same v2 APIs on both `MarketStateStore` (authoritative) and `MarketStateStoreShadow` (separate instances).

### REST source tagging

| Apply path | `BookSource` |
|------------|--------------|
| Startup seed | `REST_BOOTSTRAP` |
| Post-gap resync | `REST_RECOVERY` |
| Legacy poll loop | `REST_POLL` (deprecated; disabled at M8) |
| WS ingest (M8+) | `WEBSOCKET` with `source_quality=WS_PRIMARY` |

`REST_BOOTSTRAP` and `REST_RECOVERY` are **never** sufficient for `TRADING_ENABLED` or new entries (enforced in M3/M8).

## Files likely touched

- `src/tyrex_pm/state/market_store.py`
- `src/tyrex_pm/ingestion/market_stream.py` — pass source when writing shadow (M1) / authoritative (M8)
- `src/tyrex_pm/venue/polymarket/book_snapshot.py` — `REST_BOOTSTRAP`
- `src/tyrex_pm/venue/polymarket/clob_book_client.py` — move `fetch_order_book`; `REST_RECOVERY` path
- `tests/test_market_state_store.py`

## New files likely created

- `src/tyrex_pm/market_data/models.py` — snapshots, `BookSource`, `SourceQuality`
- `tests/test_market_state_capture.py`
- `tests/test_event_ordering_state.py`

## Contracts / interfaces

```python
class BookSource(str, Enum):
    WEBSOCKET = "websocket"
    REST_BOOTSTRAP = "rest_bootstrap"
    REST_RECOVERY = "rest_recovery"
    REST_POLL = "rest_poll"
    FIXTURE = "fixture"

class SourceQuality(str, Enum):
    WS_PRIMARY = "ws_primary"
    REST_BOOTSTRAP = "rest_bootstrap"
    REST_RECOVERY = "rest_recovery"
    REST_POLL = "rest_poll"

@dataclass(frozen=True)
class MarketStateSnapshot:
    token_id: TokenId
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    best_bid: Decimal | None
    best_ask: Decimal | None
    best_bid_size: Decimal | None
    best_ask_size: Decimal | None
    spread: Decimal | None
    mid: Decimal | None
    received_ts: datetime
    exchange_ts: datetime | None
    book_age_ms: int
    source: BookSource
    source_quality: SourceQuality
    sequence: int | None
    snapshot_id: str
    reconnect_gap: bool
```

## Config changes

```yaml
runtime:
  market_data:
    store_top_n_levels: 5
```

## Facts / observability changes

- Internal in M2; `reconnect_gap` transitions facted in M3.

## Tests to add or update

- `capture()` immutable; `snapshot_id` unique per capture
- `book_age_ms` from `received_ts`
- Sequence stored; `last_sequence` updated monotonically
- `set_reconnect_gap` / clear after apply with fresh sequence
- Pair capture requires both legs
- Existing store tests pass unchanged

## Acceptance criteria

- [ ] No consumer breaks without opt-in to new APIs
- [ ] M1 shadow store uses same v2 class, separate instance
- [ ] REST bootstrap tagged `REST_BOOTSTRAP`; recovery tagged `REST_RECOVERY`

## Risks

- Top-N vs full book — default top 5 levels; document in config

## Open questions

- `exchange_ts` field name in Polymarket payload — resolve in M1 fixture

## Definition of done

v2 APIs + ordering state implemented; backward compat tests green; `fetch_order_book` in venue layer.

## Not in scope

- DataQualityGate ([M3](milestone_3_data_quality_gate.md))
- ExecutableBookView ([M4](milestone_4_executable_book_view_and_planner.md))
- Disabling REST poll ([M8](milestone_8_ws_primary_cutover.md))
