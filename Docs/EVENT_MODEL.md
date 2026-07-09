# Tyrex_PM Event Model (Phase 2B)

**Status:** draft (M2B.0-A) · **Authority:** `Docs/Implementation/Phase 2 completion/tyrex_pm_phase2b_plan.md`

Canonical contract for market-data events at the ingestion boundary. Implementation: `src/tyrex_pm/core/events.py`, `src/tyrex_pm/ingestion/sequencer.py`.

---

## 1. MarketEvent envelope

Frozen dataclass (`schema_version` default `1`):

| Field | Type | Description |
|-------|------|-------------|
| `event_id` | `str` | Deterministic 32-char hex hash (see §4) |
| `event_type` | `EventType` | Typed discriminator |
| `market_id` | `str \| null` | Paired market id when known |
| `token_id` | `TokenId \| null` | CLOB asset id |
| `venue_cursor` | `str \| null` | Generic ordering cursor (§3) |
| `source_ts` | `datetime \| null` | Venue timestamp (UTC) |
| `recv_ts` | `datetime` | Local receive time (`core.time.utc_now`) |
| `payload` | `mapping` | Type-specific body; book events include `raw` WS message |
| `schema_version` | `int` | Envelope version |
| `connection_id` | `str \| null` | WS connection id |
| `local_counter` | `int \| null` | Per-connection monotonic counter |

Serialization: `event_to_dict()` / `event_from_dict()` — ISO8601 UTC datetimes, JSON-compatible payload.

---

## 2. EventType enum

Initial members (see `core/events.py`):

- **Book / WS:** `book_snapshot`, `book_delta`, `trade_print`, `book_quality_changed`, `ws_seq_gap`, `ws_reconnect`, `rest_recovery_used`, `out_of_order_event`
- **Lifecycle (stubs):** `market_discovered`, `market_metadata_updated`, `market_phase_changed`, `session_timer_tick`, `market_close_approaching`, `market_closed`, `resolution_observed`
- **External / clock (stubs):** `external_btc_tick`, `clock_sync`

Decision outcomes remain in **facts**, not market events.

---

## 3. venue_cursor and Polymarket book_hash

**Canonical field name:** `venue_cursor`

**Documented alias:** `venue_seq` (cross-venue generality only)

For **Polymarket**, `venue_cursor` is populated from the WS message `hash` field (book_hash). It is a **string compared lexicographically**, not a numeric sequence.

Rules (mirror `market_ws_ingest._handle_sequence`):

| Condition | Action |
|-----------|--------|
| `venue_cursor is None` | Forward book event |
| `venue_cursor == last` for token | Duplicate — drop |
| `venue_cursor < last` (string compare) | Synthesize `ws_seq_gap`; do **not** forward book event |
| `venue_cursor > last` | Forward; update last |

---

## 4. event_id determinism

```text
event_id = sha256("{event_type}|{token_id}|{venue_cursor}|{source_ts_ms}|{payload_digest}")[:32]
```

- **No `uuid4()`**
- `payload_digest` = SHA-256 hex of canonical JSON (`sort_keys=True`, separators `(",", ":")`, stable `Decimal`/`datetime`/`TokenId`/`Enum` encoding)

---

## 5. payload.raw preservation

Book events **must** retain the full WS message in `payload["raw"]` so the recorder and research pipeline can access depth beyond `MarketStateStore.store_top_n_levels` (default 5). Store projection remains capped; events carry full truth.

---

## 6. MarketSequencer

One `MarketSequencer` per market (paired YES/NO tokens share it).

### Dedup

Ring set of `event_id` (default capacity 4096). Duplicate `event_id` → silent drop.

### WS_SEQ_GAP synthesis

On out-of-order `venue_cursor`, emit synthetic `ws_seq_gap` with payload:

```json
{
  "token_id": "...",
  "last_venue_cursor": "...",
  "received_venue_cursor": "...",
  "reason": "out_of_order"
}
```

### Reorder buffer

Events with `source_ts` older than already-emitted watermark are held until `flush()`.

### Coalescing (future only)

On recorder queue overflow, contiguous `book_delta` may be coalesced into `book_snapshot` + `book_quality_changed`. **Not implemented in M2B.0-A.** Policy documented for M2B.1+.

---

## 7. Replay timestamp discipline

- Live ingest: `recv_ts = utc_now()` at boundary
- Replay: consumer "now" is the event's `recv_ts` (or `source_ts` when `recv_ts` unavailable)
- Timer-driven logic in replay uses recorded `session_timer_tick` events (emitter deferred)

---

## 8. Import boundaries

- `research/` may import `tyrex_pm.core.events` and `tyrex_pm.ingestion.sequencer`
- Live `src/tyrex_pm/` must **not** import `research/`
- M2B.0-A: no wiring in `market_ws_ingest.py` (M2B.0-B)

---

## 9. Related docs

- `Docs/Implementation/phase2b_data_backbone/spec_m2b_0_a_event_contract.md`
- `Docs/Implementation/phase2b_data_backbone/spec_m2b_0_b_ws_ingest_event_backbone.md` (next)
