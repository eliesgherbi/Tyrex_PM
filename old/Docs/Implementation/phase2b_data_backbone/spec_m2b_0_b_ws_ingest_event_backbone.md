# M2B.0-B — WS ingest emits MarketEvent behind flag

## 1. Purpose in simple terms

Wire the M2B.0-A event contract into the live WebSocket ingestion path **behind a default-off flag**. When enabled, incoming WS messages become `MarketEvent`s, pass through `MarketSequencer`, and project into `MarketStateStore`. When disabled, the **exact legacy code path** runs unchanged. Prove both paths produce identical store state on fixture streams.

## 2. Boundary

### In scope

- `ingestion/event_factory.py`: WS dict → `MarketEvent`
- `ingestion/market_stream.py`: `project_event(store, event)`
- `ingestion/market_ws_ingest.py`: flagged dual path at recv boundary
- `venue/polymarket/book_snapshot.py`: optional REST recovery events when flag on
- `runtime/config.py`: `event_backbone` config block
- Env override `TYREX_EVENT_BACKBONE`
- `tests/test_market_event_projection_equivalence.py`
- Regression: all market/WS tests flag off **and** on

### Out of scope

- `paired_binary_run.py` modifications (**forbidden**)
- Fact correlation fields (M2B.0-C)
- Recorder / `EventSink` (M2B.1-A)
- `cmd_record` CLI
- SessionRunner extraction
- Survival legacy module moves
- In-trading recorder tap
- Coalescing on queue overflow (document only; implement in M2B.1 if needed)

## 3. Current repo reality

| Path | State |
|------|-------|
| `ingestion/market_ws_ingest.py` | `run_market_ws_ingest()` recv loop (~line 263): parses messages, calls `_handle_sequence`, then `apply_market_message`. `on_raw_event` hook exists (line 187) but **`app.py` does not pass it**. |
| `ingestion/market_stream.py` | `apply_market_message`, `apply_price_change`, `apply_book_message` — direct store mutation. |
| `state/market_store.py` | `apply_book()` single writer; `store_top_n_levels=5` default. |
| `runtime/config.py` | `MarketDataConfig` with `websocket.primary_enabled`; **no `event_backbone` block yet**. |
| `runtime/app.py` | Wires `run_market_ws_ingest` in `cmd_run` (~line 1348); no event backbone flag. |
| `venue/polymarket/book_snapshot.py` | REST bootstrap calls `apply_book` directly. |
| `tests/fixtures/ws/` | `market_book.json`, `market_price_change.json` for equivalence tests. |
| M2B.0-A artifacts | Assumed complete: `MarketEvent`, `MarketSequencer`, serialization. |

## Repo-plan mismatch

| Plan assumption | Repo reality | Recommended resolution |
|---|---|---|
| Emit from `market_stream` only | Hot path is `market_ws_ingest` | Emit at recv; project via `market_stream.project_event` |
| `on_raw_event` used today | Unused in `app.py` | Do not repurpose `RawMarketEvent` hook for canonical events; use explicit event path |

## 4. Files to create

| File | Why |
|------|-----|
| `src/tyrex_pm/ingestion/event_factory.py` | Build `MarketEvent` from WS payload + recv metadata |
| `tests/test_market_event_projection_equivalence.py` | Legacy vs projection store equality |

Optional: `tests/fixtures/events/combined_ws_session.jsonl` — concatenated book + delta sequence.

## 5. Files allowed to modify

| File | Expected modification |
|------|----------------------|
| `ingestion/market_ws_ingest.py` | Branch on `event_backbone_enabled`; legacy path preserved verbatim when false |
| `ingestion/market_stream.py` | Add `project_event()`; optionally extract shared internals from `apply_market_message` |
| `venue/polymarket/book_snapshot.py` | When flag on: emit `REST_RECOVERY_USED` + `BOOK_SNAPSHOT` events through sequencer+project (or direct project) |
| `runtime/config.py` | Add `MarketDataEventBackboneConfig`; parse from YAML; env helper `event_backbone_enabled()` |

## 6. Forbidden files / modules

```text
src/tyrex_pm/runtime/paired_binary_run.py          # MUST NOT MODIFY
src/tyrex_pm/runtime/pipeline.py
src/tyrex_pm/risk/*
src/tyrex_pm/execution/*
src/tyrex_pm/strategies/*
src/tyrex_pm/survival/*
src/tyrex_pm/reporting/event_sink.py               # M2B.1-A
src/tyrex_pm/runtime/record_run.py                 # M2B.1-A
strategies/paired_binary/facts.py                  # M2B.0-C
pyproject.toml                                     # no new deps
```

`runtime/app.py` may be touched **only** to pass `event_backbone_enabled` from config into `run_market_ws_ingest` if needed — no other `cmd_run` changes.

## 7. Interfaces and contracts

### event_factory.py

```python
def build_market_event_from_ws(
    msg: dict[str, Any],
    *,
    received_ts: datetime,
    connection_id: str,
    local_counter: int,
    market_id: str | None = None,
) -> MarketEvent | None:
    """Map WS event_type book/price_change → BOOK_SNAPSHOT/BOOK_DELTA.
    payload.raw = full msg. venue_cursor = msg.get('hash')."""
```

### project_event()

```python
def project_event(
    store: MarketStateStore,
    event: MarketEvent,
    *,
    source: str = BookSource.WEBSOCKET,
) -> str | None:
    """Apply MarketEvent to store. Returns snapshot_id or None if skipped.
    BOOK_SNAPSHOT/BOOK_DELTA → existing apply_book internals.
    WS_SEQ_GAP → store.set_reconnect_gap(token_id, True)."""
```

### Config

```yaml
runtime:
  market_data:
    event_backbone:
      enabled: false              # DEFAULT
      reorder_buffer_ms: 75
      emit_rest_recovery: true
```

```python
def event_backbone_enabled(*, env=os.environ, config_flag: bool = False) -> bool:
    # Mirror market_ws_primary_enabled pattern in market_ws_ingest.py
```

Env: `TYREX_EVENT_BACKBONE=1|0|true|false|yes|no|on|off`

### Ingest integration (market_ws_ingest.py)

When `event_backbone_enabled`:

```text
parse msg → build_market_event_from_ws → sequencer.ingest → for each out_event:
  if book event: project_event(write_store, out_event)
  elif WS_SEQ_GAP: apply gap side effects (set_reconnect_gap + existing facts unchanged)
```

When **disabled**:

```text
# EXACT current path — no MarketEvent allocation
_handle_sequence → apply_market_message
```

**Critical:** When flag is false, do not instantiate `MarketSequencer` or call `build_market_event_from_ws`.

### Payload preservation

- `MarketEvent.payload["raw"]` = full WS message (all book levels for research)
- `MarketStateStore` projection still capped at `store_top_n_levels` (unchanged)

## 8. Runtime flags and rollback

| Setting | Default | Rollback |
|---------|---------|----------|
| `event_backbone.enabled` | `false` | Set false → exact legacy path |
| `TYREX_EVENT_BACKBONE` | unset → use config | Set `0` or `false` |

Legacy behavior is the default. No deployment change required to roll back.

## 9. Tests required

| Test | Must prove |
|------|------------|
| `test_market_event_projection_equivalence.py` | Fixture stream: legacy `apply_market_message` vs event path → identical `capture()` per token |
| `test_market_event_projection_equivalence.py::test_reconnect_gap_equivalent` | Out-of-order hash sets `reconnect_gap` same both paths |
| `test_event_backbone_flag_off_no_allocation` | Optional: monkeypatch ensures factory not called when disabled |

## 10. Regression tests required

**Both flag states must pass:**

```bash
# Flag off (default)
pytest tests/test_market_stream_ingest.py \
  tests/test_market_ws_ingest.py \
  tests/test_ws_primary_event_ordering.py \
  tests/test_ws_primary_reconnect_gap.py \
  tests/test_market_state_store.py \
  tests/test_paired_binary_runtime.py -q

# Flag on
TYREX_EVENT_BACKBONE=1 pytest <same list> -q
```

Also: `tests/test_market_event_sequencer.py`, `test_market_event_serialization.py` from M2B.0-A.

## 11. Acceptance criteria

- [ ] `event_backbone.enabled` defaults to `false`
- [ ] Flag off: byte-level behavior match with pre-milestone ingest (regression green)
- [ ] Flag off: no `MarketEvent` construction (review + optional test)
- [ ] Flag on: projection equivalence test 100% on combined fixture
- [ ] Flag on: regression suite green
- [ ] `paired_binary_run.py` **not** in diff
- [ ] `on_book_applied` / coordinator wake path unchanged
- [ ] No fact schema changes

## 12. Divergence risks

| Risk | Control |
|------|---------|
| Modifying `paired_binary_run.py` | Forbidden list; reviewer gate |
| Changing `_handle_sequence` semantics | Event path must delegate same rules via sequencer |
| Always allocating MarketEvent | Explicit flag-off branch with no factory call |
| Replacing legacy path entirely | Legacy branch must remain copy of current logic |
| Adding recorder tap | Out of scope |
| Storing only projected 5 levels in events | `payload.raw` must retain full WS |

## 13. Review checklist

- [ ] Default flag is false
- [ ] Diff includes `event_factory.py`, `project_event`, ingest branch, config
- [ ] Diff excludes `paired_binary_run.py`
- [ ] Equivalence test compares bids/asks/hash/reconnect_gap/source_quality
- [ ] Both flag regression commands green
- [ ] REST bootstrap emits recovery events only when flag on

## 14. Done / not done examples

**Done:**

- `TYREX_EVENT_BACKBONE=1 pytest tests/test_market_event_projection_equivalence.py` passes
- Production default scenario unchanged behavior with flag off

**Not done:**

- `trigger_event_id` on facts (M2B.0-C)
- `tyrex-pm record` writes files (M2B.1-A)
- Multi-market discovery (M2B.1-B)

## 15. Next milestone dependency

**M2B.0-C** and **M2B.1-A** depend on this milestone.

**Handoff:**

- Flagged live `MarketEvent` stream at WS boundary
- `project_event()` proven equivalent to legacy mutation
- `event_backbone` config + env override
