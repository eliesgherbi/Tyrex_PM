# M2B.0-A — Event contract skeleton + Sequencer tests

## 1. Purpose in simple terms

Define the canonical `MarketEvent` data contract and a per-market `MarketSequencer` that can order, deduplicate, and detect gaps — entirely in isolated code and tests. Nothing connects to live WebSocket ingestion yet. This milestone creates the vocabulary every later Phase 2B milestone builds on.

## 2. Boundary

### In scope

- `MarketEvent` frozen dataclass and `EventType` enum
- `compute_event_id()` deterministic hashing
- `to_dict()` / `from_dict()` JSON serialization round-trip
- `MarketSequencer` with book_hash-compatible ordering rules
- Deduplication by `event_id`
- `WS_SEQ_GAP` synthesis on out-of-order `venue_cursor`
- Reorder buffer + `flush()` drain
- Unit tests and JSONL fixtures under `tests/fixtures/events/`
- Draft `Docs/EVENT_MODEL.md`
- Move unused types from `core/events.py` → `core/runtime_events.py`
- Stub `milestones_index.md` entry update (status only, when implemented)

### Out of scope

- Wiring into `market_ws_ingest.py`
- Changes to `paired_binary_run.py`
- Fact field additions (`trigger_event_id`, etc.)
- Recorder / `EventSink` / `tyrex-pm record`
- `ingestion/event_factory.py` in production tree (test helpers inside test files OK)
- Survival legacy module moves (`survival/legacy/`)
- `zstandard`, `pandas`, `pyarrow`, `research/` dependencies
- Trading behavior change
- Live trading
- Runtime config flags
- Coalescing implementation (document policy only; optional stub test)

## 3. Current repo reality

| Path | State |
|------|-------|
| `src/tyrex_pm/core/events.py` | Contains unused internal types: `MarketBookUpdated`, `UserOrderUpdated`, `EventPayload`, etc. **Zero imports** in `src/`. |
| `src/tyrex_pm/core/bus.py` | Internal `EventBus` uses `GuruTradeSignal` etc.; unrelated to `MarketEvent`. |
| `src/tyrex_pm/market_data/models.py` | `RawMarketEvent` dataclass (lines 74–81): WS envelope with `raw_event_id`, `payload`, `received_ts` — precursor only. |
| `src/tyrex_pm/ingestion/market_ws_ingest.py` | `_handle_sequence()` (line 125): book_hash dedup/gap logic to port into sequencer tests. |
| `src/tyrex_pm/state/market_store.py` | `parse_exchange_ts()` for WS ms timestamps. |
| `src/tyrex_pm/core/time.py` | `utc_now()` — use for `recv_ts` in docs/tests. |
| `src/tyrex_pm/core/ids.py` | `TokenId` NewType. |
| `src/tyrex_pm/ingestion/sequencer.py` | **Does not exist.** |
| `tests/fixtures/ws/` | `market_book.json`, `market_price_change.json` — useful reference; M2B.0-A adds `tests/fixtures/events/`. |
| `pyproject.toml` | No zstd/pandas/pyarrow. |

### Implementation clarifications (M2B.0-A authorization)

**Clarification A — preflight grep:** Before moving types from `core/events.py`, run grep/import check across `src/`. If `core/events.py` is imported anywhere, **stop and report** — do not move types silently. Expected: zero imports in `src/`.

**Clarification B — deterministic payload digest:** `payload_digest` uses canonical JSON with `sort_keys=True`, compact separators `(",", ":")`, stable conversion for `Decimal` → string, `datetime` → ISO8601 UTC, `TokenId` → str, `Enum` → value. No `uuid4`, no process-local randomness. Digest must be identical across runs.

**Clarification C — no production event_factory:** M2B.0-A must **not** create `src/tyrex_pm/ingestion/event_factory.py`. Test helpers allowed inside test files only.

**Clarification D — no ingest import:** `market_ws_ingest.py` must not import `MarketSequencer` in M2B.0-A (M2B.0-B only).

**Clarification E — EVENT_MODEL.md:** Must document envelope, EventType, `venue_cursor`/`venue_seq` alias, book_hash lexicographic rule, `event_id` determinism, `payload.raw`, dedup, `WS_SEQ_GAP`, replay timestamps, coalescing as future-only.

## Repo-plan mismatch

| Plan assumption | Repo reality | Recommended resolution |
|---|---|---|
| `core/events.py` is empty | File has dead internal event types | Move to `runtime_events.py`; repurpose `events.py` per plan |
| Integer `venue_seq` from venue | Polymarket uses string `book_hash` | Use field name `venue_cursor`; document alias `venue_seq` in EVENT_MODEL |

## 4. Files to create

| File | Why |
|------|-----|
| `src/tyrex_pm/core/runtime_events.py` | Preserve moved dead types from old `core/events.py` for potential `EventBus` future use |
| `src/tyrex_pm/ingestion/sequencer.py` | Per-market ordering, dedup, gap detection |
| `tests/test_market_event_sequencer.py` | Proves sequencer behavior |
| `tests/test_market_event_serialization.py` | Proves event_id determinism and JSON round-trip |
| `tests/fixtures/events/ws_book_sequence.jsonl` | In-order book_hash progression |
| `tests/fixtures/events/ws_gap_sequence.jsonl` | Out-of-order hash triggering gap |
| `Docs/EVENT_MODEL.md` | Draft contract doc |

## 5. Files allowed to modify

| File | Expected modification |
|------|----------------------|
| `src/tyrex_pm/core/events.py` | Replace contents with `EventType`, `MarketEvent`, `compute_event_id`, `to_dict`, `from_dict` |

**No other production files may be modified.**

## 6. Forbidden files / modules

```text
src/tyrex_pm/ingestion/market_ws_ingest.py
src/tyrex_pm/ingestion/market_stream.py
src/tyrex_pm/runtime/paired_binary_run.py
src/tyrex_pm/runtime/app.py
src/tyrex_pm/runtime/pipeline.py
src/tyrex_pm/risk/*
src/tyrex_pm/execution/*
src/tyrex_pm/strategies/*
src/tyrex_pm/survival/*
src/tyrex_pm/reporting/*
src/tyrex_pm/state/market_store.py (no changes — only import parse_exchange_ts in tests if needed)
pyproject.toml (no new deps)
config/*
```

## 7. Interfaces and contracts

### EventType enum

```python
class EventType(str, Enum):
    BOOK_SNAPSHOT = "book_snapshot"
    BOOK_DELTA = "book_delta"
    TRADE_PRINT = "trade_print"
    BOOK_QUALITY_CHANGED = "book_quality_changed"
    WS_SEQ_GAP = "ws_seq_gap"
    WS_RECONNECT = "ws_reconnect"
    REST_RECOVERY_USED = "rest_recovery_used"
    OUT_OF_ORDER_EVENT = "out_of_order_event"
    MARKET_DISCOVERED = "market_discovered"
    MARKET_METADATA_UPDATED = "market_metadata_updated"
    MARKET_PHASE_CHANGED = "market_phase_changed"
    SESSION_TIMER_TICK = "session_timer_tick"
    MARKET_CLOSE_APPROACHING = "market_close_approaching"
    MARKET_CLOSED = "market_closed"
    RESOLUTION_OBSERVED = "resolution_observed"
    EXTERNAL_BTC_TICK = "external_btc_tick"   # stub — no emitter until M2B.2
    CLOCK_SYNC = "clock_sync"
```

### MarketEvent dataclass

```python
@dataclass(frozen=True)
class MarketEvent:
    event_id: str
    event_type: EventType
    market_id: str | None
    token_id: TokenId | None
    venue_cursor: str | None       # Polymarket: book_hash
    source_ts: datetime | None
    recv_ts: datetime
    payload: Mapping[str, Any]
    schema_version: int = 1
    connection_id: str | None = None
    local_counter: int | None = None
```

### venue_cursor / book_hash rule

> `venue_cursor` is the canonical field. Document alias `venue_seq` in `EVENT_MODEL.md`. For Polymarket, populated from WS `hash` / `book_hash`. **Not a numeric sequence.** Sequencer must preserve semantics from `market_ws_ingest._handle_sequence`:
>
> - `venue_cursor is None` → forward book event
> - `venue_cursor == last` → duplicate; drop
> - `venue_cursor < last` (lexicographic string compare) → emit `WS_SEQ_GAP`; do not forward book event
> - `venue_cursor > last` → forward; update last

### compute_event_id algorithm

```python
def compute_event_id(
    *,
    event_type: str,
    token_id: str | None,
    venue_cursor: str | None,
    source_ts: datetime | None,
    payload_digest: str,
) -> str:
    source_ms = int(source_ts.timestamp() * 1000) if source_ts else 0
    key = f"{event_type}|{token_id or ''}|{venue_cursor or ''}|{source_ms}|{payload_digest}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]
```

- **Forbidden:** `uuid4()` in `event_id`
- `payload_digest`: SHA-256 hex (64 chars) of canonical JSON via `canonical_payload_json()` — `sort_keys=True`, separators `(",", ":")`, stable `Decimal`/`datetime`/`TokenId`/`Enum` encoding; deterministic across runs

### Serialization (`to_dict` / `from_dict`)

- Datetimes → ISO8601 UTC strings (match `reporting/facts.py` `make_fact` style)
- `event_type` → string value
- `token_id` → string or null
- Book events: `payload` must support `{"raw": {<full WS message>}}` for future recorder depth
- Round-trip: `from_dict(to_dict(ev)) == ev` for golden vectors

### MarketSequencer API

```python
class MarketSequencer:
    def __init__(
        self,
        market_id: str,
        *,
        reorder_buffer_ms: float = 75.0,
        dedup_capacity: int = 4096,
    ) -> None: ...

    def ingest(self, event: MarketEvent) -> list[MarketEvent]:
        """Return 0+ consumer-ready events (ordered, deduped). May synthesize WS_SEQ_GAP."""

    def flush(self) -> list[MarketEvent]:
        """Drain reorder buffer at end of stream / test teardown."""
```

### Dedup behavior

- Ring set of `event_id` (capacity `dedup_capacity`)
- Duplicate `event_id` → drop silently (no output)

### Gap detection behavior

- On lexicographic `venue_cursor < last` for same token: synthesize:

```python
MarketEvent(
    event_type=EventType.WS_SEQ_GAP,
    event_id=compute_event_id(...),  # distinct from book event
    payload={
        "token_id": str(token_id),
        "last_venue_cursor": last,
        "received_venue_cursor": venue_cursor,
        "reason": "out_of_order",
    },
    ...
)
```

- Do not forward the offending book event

### flush behavior

- Emit all events held in reorder buffer older than `reorder_buffer_ms` relative to newest seen `source_ts`
- Called explicitly in tests; not auto-called in 0-A production (no production wiring)

## 8. Runtime flags and rollback

```text
No runtime flag required because this milestone is pure additive infrastructure.
```

Rollback: delete new files; restore `core/events.py` from git if needed. No runtime behavior exists to roll back.

## 9. Tests required

| Test file | Must prove |
|-----------|------------|
| `test_market_event_sequencer.py::test_in_order_hashes_forwarded` | Sequential `venue_cursor` values emit in order |
| `test_market_event_sequencer.py::test_duplicate_hash_dropped` | Same `event_id` / hash not emitted twice |
| `test_market_event_sequencer.py::test_out_of_order_emits_ws_seq_gap` | Lower hash emits gap, blocks book event |
| `test_market_event_sequencer.py::test_no_cursor_always_forwards` | Missing hash still forwards |
| `test_market_event_sequencer.py::test_reorder_buffer_holds_inverted_ts` | Brief inversion held then released on flush |
| `test_market_event_sequencer.py::test_flush_drains_buffer` | `flush()` emits pending events |
| `test_market_event_serialization.py::test_event_id_deterministic` | Same inputs → same `event_id` across runs |
| `test_market_event_serialization.py::test_json_roundtrip_golden` | Golden vectors survive `to_dict`/`from_dict` |
| `test_market_event_serialization.py::test_all_event_types_serializable` | Every `EventType` member round-trips |

Fixtures: build `MarketEvent` instances in tests from `tests/fixtures/events/*.jsonl` rows.

## 10. Regression tests required

```text
Full existing pytest suite must remain green.
No new runtime wiring means no flag-on/flag-off split required for this milestone.
```

Minimum sanity:

```bash
pytest tests/test_market_stream_ingest.py tests/test_ws_primary_event_ordering.py -q
```

## 11. Acceptance criteria

- [ ] `src/tyrex_pm/core/runtime_events.py` contains moved old types
- [ ] `src/tyrex_pm/core/events.py` contains `MarketEvent`, `EventType`, `compute_event_id`, serialization
- [ ] `src/tyrex_pm/ingestion/sequencer.py` implements `MarketSequencer`
- [ ] All tests in §9 pass
- [ ] `Docs/EVENT_MODEL.md` draft exists with `venue_cursor`/book_hash section
- [ ] `grep -r "from tyrex_pm.core.events import MarketBookUpdated" src/` returns nothing (old type not re-exported from new file)
- [ ] No modifications to forbidden files in §6
- [ ] No new dependencies in `pyproject.toml`

## 12. Divergence risks

| Risk | Control |
|------|---------|
| Wiring into `market_ws_ingest` early | Forbidden files list; reviewer checks diff |
| Adding `event_factory.py` production module | Out of scope; test helpers only |
| Using Pydantic for MarketEvent | Spec requires frozen dataclass |
| Numeric `venue_cursor` comparison | Must use string lexicographic compare like `_handle_sequence` |
| UUID `event_id` | Explicitly forbidden in spec |
| Moving survival legacy modules | Out of scope |

## 13. Review checklist

- [ ] Only §4–§5 files touched
- [ ] `MarketSequencer` gap rules match `_handle_sequence` semantics
- [ ] `event_id` is deterministic (test included)
- [ ] `EXTERNAL_BTC_TICK` in enum but no emitter
- [ ] `EVENT_MODEL.md` documents book_hash ≠ numeric seq
- [ ] No ingest/runtime/strategy changes
- [ ] All §9 tests present and green

## 14. Done / not done examples

**Done:**

- `MarketSequencer` passes gap fixture test ported from `test_ws_primary_event_ordering` logic
- `compute_event_id` golden test stable across two pytest invocations

**Not done:**

- `market_ws_ingest` imports `MarketSequencer` (that's M2B.0-B)
- `EventSink` writes JSONL (that's M2B.1-A)
- Facts contain `trigger_event_id` (that's M2B.0-C)

## 15. Next milestone dependency

**M2B.0-B** depends on this milestone.

**Handoff artifacts:**

- `tyrex_pm.core.events`: `MarketEvent`, `EventType`, `compute_event_id`, `to_dict`, `from_dict`
- `tyrex_pm.ingestion.sequencer`: `MarketSequencer`
- `Docs/EVENT_MODEL.md` draft
