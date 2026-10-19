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
