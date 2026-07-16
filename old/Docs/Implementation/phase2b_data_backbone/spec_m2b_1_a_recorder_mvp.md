# M2B.1-A — Recorder MVP (plain JSONL, one configured market)

## 1. Purpose in simple terms

Persist the `MarketEvent` stream to disk in **record-only mode** — a standalone `tyrex-pm record` command that subscribes to market WebSocket for **one configured BTC 5m market** and writes plain JSONL segments with a manifest. No trading, no wallet, no OMS, no risk engine. The writer must never block the ingest path.

## 2. Boundary

### In scope

- `reporting/event_sink.py` — async non-blocking `EventSink`
- `runtime/record_run.py` — record-only async main
- `runtime/app.py` — `record` subparser only
- `config/scenarios/record_btc5m_single.yaml`
- Plain **JSONL** segments (no compression)
- One-market lifecycle smoke test
- Import isolation test
- Tap via callback from `run_market_ws_ingest` (requires `event_backbone.enabled: true`)

### Out of scope

- JSONL.zst / `zstandard` (M2B.1-B)
- `ingestion/market_discovery.py` multi-market (M2B.1-B)
- In-trading tap in `cmd_run` / `paired_binary_run` (M2B.1-B)
- Heartbeat alerting (M2B.1-B)
- 24h / ≥95% coverage acceptance (M2B.1-B)
- Fact emission from record mode
- BTC external feed (M2B.2)
- `paired_binary_run.py` modifications

## 3. Current repo reality

| Path | State |
|------|-------|
| `reporting/sinks/jsonl.py` | `JsonlSink`: sync open/write/**flush per line** — pattern to **avoid** in recorder |
| `runtime/app.py` | Subparsers: `run`, `reset-state`, `live-attest` — **no `record`** |
| `ingestion/market_ws_ingest.py` | Can accept tap callback when M2B.0-B complete |
| `runtime/paired_binary_metadata.py` | Resolves token IDs from event URL — reusable for record scenario |
| `config/scenarios/` | Phase1 live scenarios exist; **no `record_btc5m_single.yaml`** |
| `pyproject.toml` | No `zstandard` |
| `tests/test_v2_import_isolation.py` | Pattern for forbidden-import scanning |

## 4. Files to create

| File | Why |
|------|-----|
| `src/tyrex_pm/reporting/event_sink.py` | Non-blocking buffered writer |
| `src/tyrex_pm/runtime/record_run.py` | Record-only entrypoint |
| `config/scenarios/record_btc5m_single.yaml` | Single-market record scenario |
| `tests/test_event_sink_never_blocks.py` | Proves emit doesn't await I/O |
| `tests/test_event_sink_segments.py` | Segment rotation |
| `tests/test_event_sink_manifest.py` | Manifest completeness |
| `tests/test_record_mode_import_isolation.py` | No trading imports |

## 5. Files allowed to modify

| File | Expected modification |
|------|----------------------|
| `runtime/app.py` | Add `record` subparser; dispatch to `cmd_record` only |
| `runtime/config.py` | Add `RecordingConfig` with `enabled`, `output_dir`, `segment_max_mb`, `segment_max_s` |
| `ingestion/market_ws_ingest.py` | Optional `on_market_event: Callable[[MarketEvent], None]` — **only if not already added in M2B.0-B** |

## 6. Forbidden files / modules

```text
src/tyrex_pm/runtime/paired_binary_run.py
src/tyrex_pm/runtime/pipeline.py
src/tyrex_pm/risk/*
src/tyrex_pm/execution/oms.py
src/tyrex_pm/execution/live_oms.py
src/tyrex_pm/state/wallet_store.py
src/tyrex_pm/ingestion/user_stream.py
src/tyrex_pm/ingestion/market_discovery.py    # M2B.1-B
pyproject.toml                                # no zstandard yet
```

`cmd_run` must **not** register record tap in M2B.1-A.

## 7. Interfaces and contracts

### EventSink API

```python
class EventSink:
    def __init__(
        self,
        output_dir: Path,
        market_id: str,
        *,
        yes_token_id: str | None = None,
        no_token_id: str | None = None,
        queue_maxsize: int = 10_000,
        segment_max_mb: int = 64,
        segment_max_s: int = 300,
        batch_size: int = 100,
        batch_flush_ms: int = 50,
    ) -> None: ...

    async def start(self) -> None:
        """Create writer asyncio.Task."""

    def emit(self, event: MarketEvent) -> None:
        """NON-BLOCKING. queue.put_nowait only. NEVER await. NEVER write disk."""

    async def stop(self) -> None:
        """Flush queue, finalize manifest."""
```

### emit() contract

- Must complete in O(1) without I/O
- On `asyncio.QueueFull`: catch, increment `manifest.dropped_events`, return (optional log)
- Must not call `JsonlSink`

### Writer task

- Batch ≤ `batch_size` events or `batch_flush_ms`
- Write JSON lines to `events-{seg:05d}.jsonl`
- Rotate when file size ≥ `segment_max_mb` or age ≥ `segment_max_s`
- Update `manifest.json` via write-temp-rename

### Segment format

```text
var/recordings/<YYYY-MM-DD>/<market_id>/events-00001.jsonl
```

Each line: `MarketEvent.to_dict()` JSON (§ M2B.0-A serialization).

### Manifest schema

```json
{
  "schema_version": 1,
  "market_id": "btc_5m_...",
  "yes_token_id": "...",
  "no_token_id": "...",
  "recording_started_ts": "ISO8601",
  "recording_ended_ts": null,
  "segments": [
    {
      "path": "events-00001.jsonl",
      "event_count": 0,
      "first_recv_ts": null,
      "last_recv_ts": null,
      "bytes": 0
    }
  ],
  "gaps": [],
  "dropped_events": 0,
  "event_types_seen": {},
  "git_sha": "...",
  "scenario": "record_btc5m_single.yaml",
  "linked_run_ids": []
}
```

### Config

```yaml
runtime:
  recording:
    enabled: true
    output_dir: var/recordings
    segment_max_mb: 64
    segment_max_s: 300
  market_data:
    event_backbone:
      enabled: true
    websocket:
      primary_enabled: true
```

### Record CLI

```bash
tyrex-pm record --scenario config/scenarios/record_btc5m_single.yaml
```

```python
# runtime/app.py
p_rec = sub.add_parser("record", help="Record market events (no trading)")
p_rec.add_argument("--scenario", required=True)
p_rec.add_argument("--repo-root", default=None)
```

### Forbidden imports (record_run.py closure)

```text
SingleWriterOMS, LiveOMS, WalletStore, RiskEngine,
process_intent_work_unit, paired_binary_run,
tyrex_pm.execution.oms, tyrex_pm.risk.engine,
tyrex_pm.state.wallet_store, tyrex_pm.runtime.pipeline
```

## 8. Runtime flags and rollback

| Flag | Default | Notes |
|------|---------|-------|
| `recording.enabled` | N/A in `cmd_run` | Only used in `cmd_record` |
| `event_backbone.enabled` | `true` in record scenario | Required for events |

Rollback: do not use `tyrex-pm record`; live `cmd_run` unchanged.

## 9. Tests required

| Test | Must prove |
|------|------------|
| `test_event_sink_never_blocks.py` | `emit()` returns while writer blocked on slow mock |
| `test_event_sink_segments.py` | Rotation creates `events-00002.jsonl` |
| `test_event_sink_manifest.py` | Manifest updated with segment stats |
| `test_record_mode_import_isolation.py` | No forbidden imports in record closure |

## 10. Regression tests required

```bash
pytest tests/test_market_event_projection_equivalence.py \
  tests/test_market_ws_ingest.py -q
```

Full suite green — record module must not break `cmd_run` imports at collection time.

## 11. Acceptance criteria

- [ ] `tyrex-pm record --help` works
- [ ] One configured market recorded pre-open → resolution+60s (manual/ops smoke)
- [ ] `events-00001.jsonl` parseable line-by-line
- [ ] `manifest.json` complete with non-zero `event_count`
- [ ] `test_record_mode_import_isolation.py` passes
- [ ] `emit()` never awaits (test proof)
- [ ] Plain JSONL only — no `.zst` files
- [ ] `paired_binary_run.py` not modified
- [ ] `cmd_run` does not auto-enable recording

## 12. Divergence risks

| Risk | Control |
|------|---------|
| Using `JsonlSink` for events | Explicit API spec forbids |
| Initializing wallet in record mode | Import isolation test |
| Wiring tap into live `cmd_run` | Out of scope |
| Adding zstd early | M2B.1-B only |
| Blocking ingest on disk | `test_event_sink_never_blocks` |

## 13. Review checklist

- [ ] `record_run.py` import scan clean
- [ ] `emit()` has no `await`, no `open()`, no `flush()`
- [ ] Scenario uses `event_backbone.enabled: true`
- [ ] No `paired_binary_run` in diff
- [ ] Manifest written on graceful stop

## 14. Done / not done examples

**Done:**

- `pytest tests/test_event_sink_manifest.py` green
- Manual record produces readable JSONL with `book_delta` events

**Not done:**

- Multi-market discovery loop (M2B.1-B)
- Compressed segments (M2B.1-B)
- Live trading run auto-records (M2B.1-B `tap_in_live`)

## 15. Next milestone dependency

**M2B.1-B** depends on this milestone.

**Handoff:**

- `EventSink` + manifest schema
- `cmd_record` + single-market scenario
- Plain JSONL archive format
