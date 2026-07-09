# M2B.0-C — Fact telemetry fields + shadow parity

## 1. Purpose in simple terms

Add **optional** fact fields that link material trading decisions to the market event that triggered them — without changing any trading behavior. When the flag is off, facts are unchanged (except normal timestamp variance). When on, decisions carry `trigger_event_id`, `event_recv_ts`, and `decision_wall_ts` for latency analysis and future replay joins.

## 2. Boundary

### In scope

- Optional fields on material decision facts: `latency_chain`, `decision_snapshot`, key survival facts
- `runtime.observability.emit_event_correlation` flag (default off)
- Track `last_wake_event_id` in ingest callback; pass through `paired_binary_run` → monitor
- Preserve all existing monotonic latency fields (`decision_ts`, `trigger_to_submit_ms`, etc.)
- `monitor_trigger` unchanged (`"poll"` | `"ws_book_update"`)
- Update `Docs/reporting_fact_model.md`
- `tests/test_event_correlation_facts.py`
- Shadow paired-binary run fact diff (flag off vs on)

### Out of scope

- Trading behavior change (entry/exit/risk decisions)
- Floor enforce promotion (Phase 1 track)
- SessionRunner extraction
- Recorder / replay
- Required fields in validators
- Removing or renaming existing latency fields
- Book data in facts

## 3. Current repo reality

| Path | State |
|------|-------|
| `runtime/paired_binary_run.py` | Sets `monitor_trigger` at line ~1868; no `trigger_event_id`. **May be modified** for passing event id only. |
| `strategies/paired_binary/observability.py` | `emit_material_decision()` builds decision snapshots + latency. |
| `strategies/paired_binary/latency.py` | `LatencyTracker.decision_ts` = `monotonic_s()` float (line 66). |
| `strategies/paired_binary/facts.py` | `emit_latency_chain`, `emit_paired_binary_tick_source`. |
| `survival/facts.py` | Survival fact payloads. |
| `reporting/schema_v2.py` | Fact type constants; `FACT_SCHEMA_VERSION = 2`. |
| `reporting/facts.py` | `make_fact()` envelope. |
| `runtime/config.py` | `ObservabilityConfig` with `emit_decision_snapshot`; **no `emit_event_correlation` yet**. |
| M2B.0-B | Assumed: `event_backbone` + `MarketEvent` at ingest. |

## Repo-plan mismatch

| Plan assumption | Repo reality | Recommended resolution |
|---|---|---|
| `monitor_trigger=None` gap | Code always sets `"poll"` or `"ws_book_update"` | Add `trigger_event_id`; do not change `monitor_trigger` semantics |
| `decision_ts` is wall-clock | `LatencyTracker.decision_ts` is monotonic | Add `decision_wall_ts`; keep `decision_ts` |

## 4. Files to create

| File | Why |
|------|-----|
| `tests/test_event_correlation_facts.py` | Asserts optional fields present when flag on; absent when off |

## 5. Files allowed to modify

| File | Expected modification |
|------|----------------------|
| `runtime/config.py` | Add `emit_event_correlation: bool = False` to observability config |
| `ingestion/market_ws_ingest.py` | Optional `on_market_event_applied` callback when correlation enabled |
| `runtime/paired_binary_run.py` | Track `last_wake_event_id`; pass to monitor tick — **minimal diff only** |
| `runtime/app.py` | Wire callback from ingest to run loop if needed |
| `strategies/paired_binary/monitor.py` | Accept optional correlation kwargs |
| `strategies/paired_binary/observability.py` | Add fields to emitted facts |
| `strategies/paired_binary/latency.py` | Add optional fields to `LatencyChain.to_payload()` |
| `strategies/paired_binary/facts.py` | Pass correlation into `emit_latency_chain`, decision facts |
| `survival/facts.py` | Optional correlation on material survival facts |
| `reporting/schema_v2.py` | Comments documenting optional fields (no version bump required) |
| `Docs/reporting_fact_model.md` | Document new optional payload keys |

## 6. Forbidden files / modules

```text
src/tyrex_pm/risk/engine.py
src/tyrex_pm/execution/planner.py
src/tyrex_pm/execution/oms.py
src/tyrex_pm/runtime/pipeline.py          # no intent path changes
src/tyrex_pm/reporting/event_sink.py     # M2B.1-A
src/tyrex_pm/runtime/record_run.py
config/scenarios/*_enforce.yaml          # no enforce changes
```

## 7. Interfaces and contracts

### New optional fact payload fields

| Field | Type | When present |
|-------|------|--------------|
| `trigger_event_id` | `str \| null` | Flag on + material decision |
| `event_recv_ts` | ISO8601 `str \| null` | Flag on + material decision |
| `decision_wall_ts` | ISO8601 `str \| null` | Flag on + material decision |

### Preserved fields (do not remove/rename)

- `decision_ts` (monotonic float in latency tracker)
- `trigger_to_submit_ms`, `submit_to_ack_ms`, `trigger_to_fill_ms`
- `monitor_trigger` on survival monitor facts
- `tick_source` on `paired_binary_tick_source`

### Config

```yaml
runtime:
  observability:
    emit_event_correlation: false   # DEFAULT
```

Requires `event_backbone.enabled: true` for non-null `trigger_event_id` in practice (document in EVENT_MODEL).

### Wiring

```text
market_ws_ingest (book applied)
  → on_market_event_applied(MarketEvent)
  → paired_binary_run.last_wake_event_id = event.event_id
  → monitor.tick(..., trigger_event_id=..., event_recv_ts=...)
  → observability.emit_material_decision adds decision_wall_ts=utc_now()
```

Only set `last_wake_event_id` for events that drive coordinator wake (book snapshot/delta applied to authoritative store).

### Validator backward compatibility

- `scripts/validate_paired_binary_phase2_live_run.py` — must not require new fields
- `scripts/validate_m8_ws_primary_run.py` — same
- New fields are **additive**; absence when flag off is correct

## 8. Runtime flags and rollback

| Flag | Default | Rollback |
|------|---------|----------|
| `emit_event_correlation` | `false` | Set false → facts omit new fields |

Combined with `event_backbone.enabled: false` → no correlation possible (all null/absent).

## 9. Tests required

| Test | Must prove |
|------|------------|
| `test_event_correlation_facts.py::test_flag_off_omits_fields` | Payloads lack `trigger_event_id` key or value is null |
| `test_event_correlation_facts.py::test_flag_on_populates_fields` | Material decision facts have non-null `trigger_event_id` in shadow harness |
| `test_event_correlation_facts.py::test_monotonic_fields_preserved` | `decision_ts` still monotonic float |
| `test_event_correlation_facts.py::test_same_decisions_flag_off_on` | Same number of intents/state transitions shadow run |

## 10. Regression tests required

```bash
pytest tests/test_paired_binary_runtime.py \
  tests/test_event_driven_survival_monitor.py \
  tests/test_latency_facts.py \
  tests/test_paired_binary_phase2_regression.py -q
```

Run with `emit_event_correlation` false (default) and true (fixture override).

Validators (manual or CI):

```bash
# Must still pass on old fact fixtures without new fields
python scripts/validate_paired_binary_phase2_live_run.py <existing run dir>
```

## 11. Acceptance criteria

- [ ] `emit_event_correlation` defaults to `false`
- [ ] Flag off: fact types and decision counts unchanged vs baseline
- [ ] Flag off: new fields absent from payloads
- [ ] Flag on (shadow): material decisions have `trigger_event_id` populated when WS wake occurred
- [ ] `decision_ts` monotonic behavior unchanged
- [ ] No changes to risk/OMS/pipeline decisions
- [ ] `reporting_fact_model.md` updated
- [ ] Existing validators pass without modification to require new fields

## 12. Divergence risks

| Risk | Control |
|------|---------|
| Changing exit/stop thresholds | Only fact payload fields |
| Making fields required in validators | Explicit backward-compat rule |
| Replacing `decision_ts` with wall-clock | Add `decision_wall_ts` separately |
| Putting book data in facts | Forbidden — only event IDs and timestamps |
| Large `paired_binary_run` refactor | Minimal diff: one state variable + pass-through |

## 13. Review checklist

- [ ] Diff limited to observability/monitor/facts/config/ingest callback
- [ ] No `risk/engine.py` or `execution/oms.py` changes
- [ ] Flag default false
- [ ] `LatencyTracker.decision_ts` still `monotonic_s()`
- [ ] Validator scripts run clean on pre-milestone fact files

## 14. Done / not done examples

**Done:**

- Shadow run flag-on emits `trigger_event_id` on `latency_chain` for stop trigger
- Flag-off run produces identical fact_type sequence as before

**Not done:**

- Replay joins facts to events (M2B.5)
- Recorded `strategy_events` Parquet table (M2B.3)

## 15. Next milestone dependency

**M2B.5** (replay-diff joins) depends on correlation fields from this milestone.

**M2B.1-A** does not require M2B.0-C (recorder can start after M2B.0-B).

**Handoff:**

- Optional fact correlation contract documented
- Shadow proof of no behavior change
