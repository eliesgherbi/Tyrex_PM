# Recording framework (integration plan P4)

Status: implemented 2026-10-19. This covers decision and explain recording. Raw-input capture is P5.
Plan: `research_live_feature_integration_plan_2026-10-19.md`, sections 0.4 and 0.6.

## What is recorded, where

| Class | Content | Store | Optional |
|---|---|---|---|
| essential | execution journal, settlement ledger, `run_summary.json` | sqlite / json | never |
| operational | run evidence (`run_events`) | sqlite, bounded queue | never |
| decisions | one record per evaluation, including host skips | `<run>/recording/decisions/part-*.jsonl.zst` | `recording.level >= decisions` (default) |
| explain | indicator `explain()` state behind selected decisions | `<run>/recording/explain/...` | `recording.level: explain` |
| raw inputs | feed envelopes | P5 | `recording.raw_inputs` |

A decision record has:

- the host fields: schema, mode, run_id, window_id, at_ns, tau_s, action, reason, references;
- the strategy's declared fields (`StrategyPlugin.decision_record_schema`, which for settlement_value is `settlement_value_decision_v2` with 81 fields);
- `inputs`, which gives the timing of every input at decision time:
  - per-leg book source/receive/apply times, data age, sync health and book version;
  - book connection epoch;
  - freshness assessments for the books and the reference;
  - Binance and Chainlink source timestamps;
  - the clock view (status, offset, uncertainty, snapshot id).

A row field that is not declared is not recorded. It is listed under `undeclared_fields` in the recording status, so a new diagnostic is never silently absorbed or lost.

### Explain selection (decision 5)

The explain trigger has three settings:

- `entry_band` (default) records every evaluation inside the strategy's entry band. For entries, vetoes, holds and data faults it also writes up to `explain_context_evaluations` (10) preceding evaluations, each marked `context_for_at_ns`.
- `actionable` records only those actionable events.
- `all` records every evaluation; use it for short diagnostic runs.

## Hosts

| Host | Decisions | Explain |
|---|---|---|
| Live and paper (`TradingRuntime`) | Recorder stream. The `STRATEGY_DECISION` journal event keeps a compact row (`decision_row_compact: true`). | Recorder stream |
| Observation | `decisions.jsonl` as before, plus `inputs` | Recorder stream when `recording.level: explain` |
| Replay (`run_window(recording=...)`, `Lab.run(recording=...)`, `RunSpec.recording`) | `inputs` added to the decision rows | `result["explanations"]` and batch `explanations.parquet` |

Replay recording is opt-in. Without it, bundles and batch part digests are unchanged. With it, the decision digest is identical (tested in `test_replay_recording_adds_evidence_without_changing_behaviour`).

## Configuration (`recording:` in run and observation YAML)

```yaml
recording:
  level: decisions          # essential | decisions | explain
  explain_trigger: entry_band   # entry_band | actionable | all
  explain_context_evaluations: 10
  raw_inputs: off           # P5
  raw_on_fault: stop_capture   # stop_capture | stop_entries (strict runs)
  queue_max_records: 50000  # per stream
  queue_max_mib: 64         # per stream, estimated
  segment_max_mib: 64
  segment_max_seconds: 300
  overflow: drop_newest     # drop_newest | drop_oldest
  shutdown_timeout_s: 10
  retention_days: 30        # applied only by `tyrex-pm recording prune`
```

## Performance design (amendment 4)

### Capture on the trading loop
The host builds fresh dicts of primitives (decision-time copies). The recorder appends them under a lock to a bounded deque. It never serializes, never does I/O and never blocks.

Queue memory is accounted per stream with an O(1) size hint. The hint is:

- seeded by one structural estimate;
- re-estimated every 256th record;
- raised by the writer to 3× the actual JSON size.

Explanations are therefore counted at their real size. The full structural estimate costs about 90 µs per decision row, which is why it is not computed per record.

### Writer
A single daemon thread serializes JSON, writes zstd segments and rotates them by size or age. Each closed segment gets a manifest with its record count, capture-time range and sha256. Its CPU time is reported as `writer_cpu_s`.

The writer drains every 0.25 s, or early once a stream has 512 queued records. Waking it on every capture made it compete with the trading loop for the GIL.

### Accounting
Every record ends up in exactly one bucket:

- `written`;
- `dropped_overflow`;
- `dropped_after_failure`;
- `serialize_failed`;
- `dropped_at_shutdown` (still queued at close);
- `unconfirmed_at_shutdown` (held by a writer that missed the close deadline);
- `lost_in_failed_segment` (in a segment that could not be closed; moved out of `written`).

Gaps are kept as capture-time intervals per cause.

### Failure exposure
Status is read from the recorder object and does not depend on the writer. A dead writer, or a failed stream, shows as `failed`.

The live runtime records:

- `RECORDING_STARTED`;
- `RECORDING_STATUS` every 60 s, with counters, capture cost and run-evidence queue state;
- `RECORDING_FAILED` once, saying that trading continues;
- `RECORDING_CLOSED`.

`run_summary.json` has a `recording` section, and `watch-run` shows one recording line.

### Shutdown
`close(timeout)` stops intake and drains until the timeout. Whatever remains is counted, as listed above. Shutdown takes at most `shutdown_timeout_s`.

### Run evidence
Fixes in the same phase:

- The timestamp is now taken at capture, not at write. The `run_events_meta` table records `schema_version: 2` and `observed_at_basis: capture`.
- Per-evaluation events (`STRATEGY_DECISION`, `EVALUATION_SKIPPED`) are bounded at 20,000 queued. Drops are counted and reported as a reporting failure. Other event types are never dropped.
- `run_summary.json` (schema 6) no longer embeds per-evaluation events. It reports them in `run_evidence_omitted`, and they stay in sqlite.

## Measurements (decision 7: measure, then set limits)

Command: `tyrex-pm recording benchmark` (`recording/benchmark.py`). Results are in `var/research/recording_benchmark_20261019/`.

Setup:

- Windows 11, Python 3.12;
- decision records of 3.7 KB JSON (the 81 declared fields plus `inputs`);
- one 1.0 KB explain record per decision (two EWMA estimators).

Synthetic capture, paced at 2 ms between evaluations (2,000 evaluations, 4,000 records):

| Case | Capture p50 / p99 / max (µs) | Writer CPU (s) | Written / dropped | Notes |
|---|---|---|---|---|
| normal | 20.7 / 59.7 / 416 | 0.19 | 4000 / 0 | 49 B compressed per record |
| tracemalloc pass | 52.9 / 127 / 781 | 1.14 | 4000 / 0 | peak traced 0.84 MiB |
| slow disk (2 ms per record write), 2 s close | 20.4 / 64.7 / 513 | 0.23 | 2094 / 1906 | drops counted, capture cost flat |
| full disk (writes fail) | 19.8 / 73.8 / 255 | 0.00 | 0 / 4000 | `failed: true`, error visible, capture cost flat |

Burst of 20,000 evaluations with no pause (stress):

- p50 is 8.6 µs.
- 17,254 records are dropped, because the per-stream 64 MiB estimated bound holds about 6,000 decision records. This is the bound working as designed.
- The maximum is about 40 ms, from GIL switching while the writer serializes 2,000-record batches. This does not occur at live rates.

The full host path is the decision projection, input timing and explain capture, measured with `DecisionRecording` at about 1 ms pacing:

| Level / trigger | Capture p50 / p99 (µs) |
|---|---|
| decisions | 52 / 141 |
| explain, entry_band | 75 / 250 |
| explain, all | 71 / 266 |

Live evaluates 1–4 times per second, so this is under 0.03% of one core. The components are `input_timing` (16 µs), the schema projection (8 µs) and the queue append (about 7 µs). The rest is cache and GIL effects after an idle gap.

Storage is about 25–50 B compressed per record, about 4 MB per day per stream at 1 evaluation per second.

### Limits set from these measurements
- The defaults above (50,000 records, 64 MiB per stream) hold more than 1.5 h of live decisions if the writer stalls.
- Watch `capture_cost_us.p99` in `RECORDING_STATUS`. A sustained p99 above 1 ms in a live run is a regression to investigate.
- Submission latency and loop delay are measured by the existing `EventLoopLagMonitor` and order timings. Comparing them with recording on and off is part of the P7 qualification run, not something to infer here.

## Maintenance
`tyrex-pm recording prune --root var/runs [--older-than-days 30] [--apply]`

- It is a dry run unless `--apply` is given.
- It removes only `recording/` folders whose `manifest.json` was written by this recorder.
- Run summaries, journals, ledgers and the execution database are never touched.

## Raw input capture (P5)

`recording.raw_inputs: on` adds a `raw` stream to the same recorder, so it has the same bounds, accounting, status, close and prune behaviour. `recording/raw.py` (`RawCapture`) is passed to `run_market_data_runtime(raw_capture=...)` by both the live runtime and observation.

### What is captured, and how faithfully

| Input class | Where | Fidelity |
|---|---|---|
| `reference_twap` | RTDS adapter, frame before parsing (incl. subscription history) | `byte_exact` |
| `binance_trades` | Binance trade adapter, frame before parsing | `byte_exact` |
| `binance_depth` | Binance depth adapter (only when in the input contract) | `byte_exact` |
| `clob_market` | `BindingFeed._handle_sdk_event`, before normalization | `sdk_model` (JSON dump of the SDK event) |
| `clob_rest_book` | REST bootstrap/resync fetcher | `sdk_model` |
| `market_metadata` | Gamma discovery `event_fetcher` | `sdk_model` (SDK-converted event dict) |
| `clock` | every clock snapshot applied to the time authority | `host_event` |
| `connections` | RTDS/Binance health callbacks, CLOB connect/disconnect per epoch | `host_event` |
| `bindings` | active/prepared feed bindings, promotions | `host_event` |
| `evaluations` | each evaluation instant (trigger, monotonic time, window) | `host_event` |

Each record carries:

- `source`, `stream` and `connection_id`;
- `received_at` (wall time, ns) and `received_monotonic_ns`;
- `transport`, `body_kind`, and the body as `body_text`, `body_base64` or `body_json`;
- a capture sequence number.

The CLOB websocket, REST book and Gamma reach Tyrex only as typed objects from the Polymarket SDK. Their venue bytes do not exist in Tyrex code, so they are labelled `sdk_model` rather than presented as raw.

### Completeness manifest (`recording/raw_completeness.json`)

The manifest gives, per input class: records captured, fidelity, and whether the class is outside the strategy's input contract. It also reports missing classes and capture faults. Its fields:

- `inputs_captured`: every expected class was captured, with no fault and no drop.
- `decision_complete`: replay decision for decision from this capture. **Currently `false`**, for these reasons, each listed in the manifest:
  - the warehouse has no RTDS (`crypto_prices_twap_sixty`) normalizer, and replay reads the PolyBolt reference series;
  - the CLOB and Gamma captures are SDK models, while the warehouse normalizers expect venue JSON;
  - live Binance depth is `@depth20`, while the warehouse normalizes `@depth` diffs.
- `fill_simulation`: fills need execution and account state, which is in the execution journal, not in raw capture.

Decision reconstruction (feeds, warm-up, clock, connections, metadata, evaluation instants) and fill simulation are therefore reported as separate questions (amendment 3).

### Fault policy (decision 6)

A raw fault is any of: a capture exception, a failed writer, or any dropped raw record.

- `raw_on_fault: stop_capture` (default): capture stops, `RAW_CAPTURE_FAILED` is recorded, and trading continues.
- `stop_entries` (strict qualification runs): additionally sets the entry-only blocker `RAW_CAPTURE_FAILED`. Exits, holds and position management are unaffected.

### Storage, measured (signal-only observation, 2026-10-19, 3.5 min, one rollover)

| Stream | Records | Uncompressed | Compressed |
|---|---|---|---|
| raw | 135,247 | 142 MB | 11.1 MB |
| explain (`entry_band`) | 93 | 24 KB | 1.5 KB |

Raw records by class:

| Class | Records |
|---|---|
| CLOB market | 131,630 |
| Binance trades | 3,157 |
| RTDS | 199 |
| evaluations | 197 |
| clock | 38 |
| connections | 12 |
| REST book | 7 |
| bindings | 4 |
| Gamma | 3 |

Results:

- Writer CPU was 3.4 s over 212 s (~1.6% of one core).
- No drops.
- Decision capture cost was p50 58 µs and p99 150 µs per evaluation.

Extrapolation: about 190 MB/hour, or about 4.5 GB/day, compressed. About 97% of that is the CLOB SDK event dumps (~620 events/s across two bindings).

Consequences:

- Keep raw capture for observation and diagnostic trials, as decided, with `retention_days` and `recording prune`.
- Do not leave raw capture on in continuous live runs without disk monitoring.
- The default queue bound (64 MiB estimated) absorbs roughly 20–40 s of raw input if the disk stalls. After that, drops are counted, and under `stop_entries` new entries stop.
