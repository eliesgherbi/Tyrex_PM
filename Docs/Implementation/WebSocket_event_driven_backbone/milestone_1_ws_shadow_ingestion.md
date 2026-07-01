# Milestone 1 — WebSocket Shadow Ingestion

**Status:** Group A implemented (shadow-only) — see [`ws_fixture_spike.md`](ws_fixture_spike.md)

## Objective

Wire **Polymarket market WebSocket** ingestion in **shadow mode**: WS updates a **separate shadow store** and emits comparison facts; **REST remains authoritative** for all live trading decisions.

**This milestone is temporary validation only.** Production target is WS-authoritative cutover in [M8](milestone_8_ws_primary_cutover.md). The project must not stop at M1.

## Why this milestone exists

Parsers exist (`ingestion/market_stream.py`) but no transport. M1 validates WS payloads, reconnect, event ordering, and freshness **without** risking live trades on unproven data.

## Current codebase state

**Implemented (M1 Group A):** `ingestion/market_ws_ingest.py` shadow loop; `coord.market_state_shadow`; comparison facts; REST authoritative. Enable via `TYREX_MARKET_WS_SHADOW=1` or `runtime.market_data.websocket.shadow_enabled`.

**Confirmed:** `apply_market_message` supports Polymarket `price_changes[]` wire format. REST poll remains authoritative in `app.py`.

## Target behavior

### Shadow store architecture (fixed policy)

```text
REST → MarketStateStore (authoritative — all live decisions)
WS   → MarketStateStoreShadow (separate instance — comparison only)
Facts: ws_vs_rest_book_compare (age, touch, depth delta)
Feature flag: TYREX_MARKET_WS_SHADOW=1
No live paired-binary decision may read MarketStateStoreShadow in M1.
```

**Rationale:** A separate shadow store prevents accidental live trading from unvalidated WS data; makes the shadow/authoritative boundary explicit; reduces migration risk.

At M8 cutover: WS writes to **authoritative** `MarketStateStore`; shadow instance removed or unused; REST poll disabled in healthy steady state.

### Event ordering (deterministic)

Every WS connection tracks:

```text
connection_id
local_event_counter (increment on each message)
received_monotonic_ns
```

**If payload provides sequence/hash:**

```text
store last_sequence/hash per token in shadow store metadata
if sequence <= last_sequence:
    ignore event; emit out_of_order_event fact
if gap detected (sequence jump):
    set reconnect_gap=true on shadow path (for validation)
    log gap; trigger REST_RECOVERY on authoritative store for comparison
    do not apply WS delta to shadow until REST resync completes
```

**If payload does NOT provide sequence/hash:**

```text
apply events in received_monotonic_ns order to shadow store
after any WS reconnect:
    always REST_RECOVERY bootstrap on authoritative store
    restart shadow ingest; block shadow "ready" until fresh WS book received
    (authoritative entries still blocked until M3/M8 readiness — M1 does not enable entries from WS)
```

On WS reconnect during M1: authoritative store uses REST bootstrap/recovery; shadow resumes after reconnect; comparison facts continue.

## Files likely touched

- `src/tyrex_pm/ingestion/market_stream.py` — add `run_market_ws_ingest`
- `src/tyrex_pm/runtime/app.py` — spawn shadow task when flag set
- `src/tyrex_pm/runtime/health_runtime.py` — `mark_market_ws_message`
- `src/tyrex_pm/runtime/market_data_runtime.py` — hold `authoritative_store` + `shadow_store` handles
- `src/tyrex_pm/reporting/schema_v2.py` — new fact types
- `src/tyrex_pm/venue/polymarket/market_ws.py` — delete stub or delegate

## New files likely created

- `src/tyrex_pm/state/market_store_shadow.py` — thin wrapper or alias `MarketStateStoreShadow = MarketStateStore` (separate instance)
- `src/tyrex_pm/ingestion/market_ws_ingest.py` (optional split)
- `src/tyrex_pm/market_data/models.py` — `RawMarketEvent`, `NormalizedBookEvent`, `NormalizedPriceChangeEvent`
- `tests/fixtures/ws/market_book.json`, `market_price_change.json`
- `tests/test_market_ws_ingest.py`
- `tests/test_ws_rest_shadow_compare.py`
- `tests/test_shadow_store_isolation.py` — **proves live code path never reads shadow**

## Contracts / interfaces

```python
@dataclass(frozen=True)
class RawMarketEvent:
    raw_event_id: str
    channel: str
    payload: dict
    received_ts: datetime
    received_monotonic_ns: int
    connection_id: str
    local_event_counter: int

async def run_market_ws_ingest(
    coord,
    token_ids: list[str],
    *,
    stop: asyncio.Event,
    shadow_store: MarketStateStore,  # required separate instance — NOT optional None
    authoritative_store: MarketStateStore,  # read-only for compare; never written by WS in M1
    on_raw_event: Callable[[RawMarketEvent], None] | None = None,
) -> None: ...
```

## Config changes

```yaml
runtime:
  market_data:
    websocket:
      shadow_enabled: true
      primary_enabled: false   # M8 only
      url: ""
      reconnect_backoff_s: 3.0
      subscribe_batch_size: 50
```

Env: `TYREX_MARKET_WS_SHADOW=1`

## Facts / observability changes

- `market_ws_connected` / `market_ws_disconnected`
- `ws_vs_rest_book_compare` — rest_age_ms, ws_age_ms, bid_delta, ask_delta, depth_delta
- `out_of_order_event` — token, sequence, last_sequence
- `ws_sequence_gap_detected` — token, expected, received
- `raw_event_received` (sampled)

## Tests to add or update

- Parse fixture payloads → **shadow store** updated; authoritative unchanged by WS
- Reconnect invokes backoff; REST recovery on authoritative after gap
- Comparison fact emitted when shadow fresher than REST
- **`test_shadow_store_isolation`:** grep/wrap ensures `paired_binary_run` / `entry_eval` / `monitor` only receive authoritative store handle
- Out-of-order sequence ignored + fact emitted
- Gap detection sets flags + triggers REST resync on authoritative

## Acceptance criteria

- [x] Shadow ingest updates separate store (fixtures + unit tests)
- [x] Authoritative store unchanged by WS in M1
- [x] **Zero** paired-binary code paths read shadow (`test_shadow_store_isolation`)
- [x] Event ordering: hash dedup + gap facts on disconnect
- [x] Existing test suite green (Group A tests)
- [x] Documented WS payload samples in `tests/fixtures/ws/`
- [ ] Shadow WS runs ≥30 min in staging without crash (operational soak)

## Risks

- Wrong subscribe format — mitigated by [`ws_fixture_spike.md`](ws_fixture_spike.md)
- Engineers accidentally pass shadow store to strategy — **enforced by isolation tests**

## Open questions

- ~~Exact Polymarket market WS URL and auth~~ — **Resolved:** [`ws_fixture_spike.md`](ws_fixture_spike.md)

## Definition of done

Shadow ingest operational; separate shadow store enforced; comparison + ordering facts in runs; REST sole authority for decisions; team agrees ready for M2–M7 stack before M8 cutover.

## Not in scope

- WS-primary cutover ([M8](milestone_8_ws_primary_cutover.md))
- DataQualityGate blocking ([M3](milestone_3_data_quality_gate.md))
- Writing WS to authoritative store
- Strategy logic changes
