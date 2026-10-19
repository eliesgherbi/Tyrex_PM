# Integration plan: recording, volatility time policy, disagreement veto, notebook 06

Status: **approved with amendments (2026-10-19)** for implementation of phases P0–P6.
Research qualification (P7) and live enablement (P8) remain separate approvals. Section 0
records the amendments and decisions; where it conflicts with later sections, section 0
wins. Original text below is kept for the review history.

## 0. Approved amendments and decisions

### 0.1 Migration acceptance compares behaviour, not identity

The replay `decision_digest` includes the configuration hash. Moving volatility into the
configuration intentionally changes that hash for non-default policies. Acceptance
therefore compares a **normalized behaviour record** separately from identity metadata:

- decisions: `(at_ns, action class, chosen_side, intended shares, limit price)`; reason
  strings normalized through an explicit mapping (the study's host `BLOCKED` with
  `STUDY_DISAGREEMENT_VETO` / `STUDY_COMPARISON_UNAVAILABLE` equals the strategy's
  `WAIT` with `DISAGREEMENT_VETO` / `DISAGREEMENT_INPUT_INVALID`);
- execution: attempts, POSTs, fills (side, shares, price), economics;
- identity metadata (config hash, provenance, code identity) is reported, not compared.

### 0.2 Migration is proven under the old execution contract first

Step 1: the new configuration path, **old execution contract
(`replay_execution_contract_v1`)**, must reproduce the saved notebook 06 behaviour.
Step 2: only then, the new contract (`v2`, replay final gate with feature readiness) is
measured as a separate change. A changed result is attributable to exactly one of the two.

### 0.3 Complete replay capture is defined, not assumed

Raw feed messages alone do not reproduce a run. A capture is **decision-complete**
only if it holds, for the captured interval:

| Element | Why |
|---|---|
| Feed messages as received (Binance trades, RTDS TWAP incl. subscription history, CLOB book stream) | Model and book inputs |
| Initial and warm-up state (REST book snapshots, indicator warm-up interval before the first decision, sealed opening prices) | Indicators and books start from the same state |
| Clock samples and the clock policy | Corrected decision time |
| Connection events (connect, subscribe, reconnect, generation) | Gap and reconnect behaviour |
| Market metadata (bindings, tick size, minimum size, fee schedule) | Constraints and sizing |
| Evaluation timing (actual evaluation instants) | Same decision instants, not a resampled grid |
| Execution and account state (journal, account snapshots used by gates) | Gate decisions and position context |

Two capabilities are distinguished: **decision reconstruction** (replaying the trader's
own decisions from its inputs) and **counterfactual fill simulation** (alternative
fills, which also need book depth over time and remains a model). A capture manifest
states which elements are present, so a run is labelled decision-complete or not.

### 0.4 Recording performance is designed and measured

- **Decision-time values:** capture stores a *copy* of primitive values (the driver
  already builds a fresh row per evaluation; indicators return a new mapping of floats
  from `explain()`). Nothing in a queue references live, mutable indicator state.
- **Memory accounting:** each record carries an estimated size (cheap structural
  estimate at capture); queue bounds are in records **and** bytes and include
  variable-sized explanations.
- **CPU:** serialization runs in one background thread but still competes for the GIL;
  writer thread CPU time is measured (`time.thread_time`) and reported.
- **Bounded shutdown:** `close(timeout_s)` stops intake, drains up to the timeout, and
  counts the remainder as `dropped_at_shutdown`.
- **Independent failure exposure:** counters live in the recorder object, updated by
  capture and writer; the trading loop reads them and reports through run evidence and
  the run summary, so a dead writer is still visible.

### 0.5 Identity compatibility rules are explicit

- No general rule omits defaults. A **closed list** of fields added after identities
  were frozen is omitted from the identity payload **only while equal to their legacy
  value** (`volatility.variance_time_policy = per_sample_v0`,
  `disagreement_veto` section at its disabled default, replay `execution_contract = v1`).
  Any future field must be added to that list deliberately, with a test.
- Behaviour-changing settings (any non-legacy value) are always part of the identity.
- The **full effective configuration** (every field, defaults included) is saved
  separately in provenance, so nothing is hidden by the identity rule.

### 0.6 Decisions

| # | Decision |
|---|---|
| 1 | `volatility.variance_time_policy`: `per_sample_v0` (default) \| `elapsed_variance_v1` |
| 2 | Veto compares the **point** probability with the **UP two-sided midpoint** (reproduces the study). A conservative-bound comparison is a separate future hypothesis. |
| 3 | Pre-send recheck is an explicit option `recheck_at_dispatch`, **off** for migration. A refusal there is a `NOT_SENT` execution outcome (the attempt already started). |
| 4 | Retry elapsed time keeps running while vetoed; before the first attempt no retry episode exists. |
| 5 | `explain` trigger default `entry_band`, plus bounded preceding context for entries, vetoes and data faults; `all` for short diagnostic runs. |
| 6 | Raw capture on for observation and diagnostic live trials; measure storage before setting a budget. On capture failure, capture stops visibly; in strict (qualification) runs new entries stop cleanly while position management continues. |
| 7 | Measure first, then agree limits on submission latency, event-loop delay, CPU, memory and slow/full-disk behaviour. |
| 8 | Parity correction applies to new experiments; historical results keep their original contract. |

Three tracks are kept separate throughout:

| Track | Meaning | Gate to start |
|---|---|---|
| **Implementation** | Code that makes a feature available and identical in live, observation and replay. Defaults preserve current behaviour. | Approval of this plan |
| **Research qualification** | Evidence that a feature setting is worth using, on development then untouched windows. | Implementation phases accepted |
| **Live enablement** | Turning a qualified setting on in a live template, after observation and a supervised trial. | Qualification accepted |

---

## 1. What exists today

### 1.1 Components to reuse

| Area | Component | Reuse as |
|---|---|---|
| Strategy config | `strategies/settlement_value/config.py` (`SettlementVolatilityConfig`, `SettlementEntryConfig`), `schema.py` (`settlement_value_config_from_parameters`, `settlement_value_config_as_parameters`, `_SECTIONS`) | The single parser for live, observation and replay. New settings go here. |
| Indicator declaration | `IndicatorSpec` (`indicators/spec.py`), `ewma_vol_spec` (`settlement_value/plugin.py:78`), `plugin_indicator_specs` (`strategies/registry.py:68`), `sigma_config_from_spec` (`indicators/graph.py:73`) | The only path from strategy config to the estimator, used by `MarketSessionRuntime.create` in both live (`trading_runtime.py`) and replay (`strategy_replay/run.py`). Rebuilt on reconnect (`graph.py` `invalidate_source`). |
| Estimator | `EwmaVolatilityEstimator`, `SigmaConfig` (`indicators/ewma_volatility.py`) | Already contains the uncommitted `time_policy` / `elapsed_variance_v1` math. |
| Decision logic | `SettlementValueDriver.evaluate` / `_observe` (`settlement_value/driver.py`) | The one place where probability, side and size are computed; same driver in all modes. |
| Decision records | `runtime/decision_records.py` (`base_record`, `evaluated_record`, `skipped_record`, `settlement_decision_record_v1`) | Shared by replay and observation; to be generalized and used by live too. |
| Run evidence | `RunEvidenceRecorder` + `SqliteRunEvidenceJournal` (`persistence/run_evidence_journal.py`), `run_events` | Operational evidence journal; keep, but bound and version it. |
| Essential evidence | `SqliteExecutionJournal` (`execution_events`, FULL sync, dedupe), `SettlementLedger` (jsonl, fsync per record) | Unchanged. Never part of an optional recording level. |
| Raw capture | Warehouse `RawEnvelope` (`data_warehouse/contracts.py`), `RawWriter` (`capture/writer.py`: byte-bounded intake, group fsync, sealed `jsonl.zst` segments with manifests), normalizer `delivery_kind` | The raw-input format and writer for live/observe capture, so recorded runs become replayable warehouse collections. |
| Observation | `ObservationRecorder` (`runtime/observation.py`): bounded buffer, drop counting, flush policy | Its bounded/dropping semantics are the model for diagnostics; its writer is replaced by the shared pipeline. |
| Research tooling | `staged.py` (`code_identity`, `replay_identity`, `experiment_id`, `effective_parameters`, `EDITABLE`), `batch.py` (`RunSpec`, `run_batch`, resumable parts, `summaries.parquet` / `decisions.parquet`), `v3.py` (`Candidate`, `frozen_windows`, `Verification`, `FeatureRun`, `feature_vs_baseline`, `reference_policy`), `explorer.py` (`Explorer`, `prepare`, `DatasetSpec`, comparison and window plots), `timelines.py` (`get_trace`, `plot_timeline`) | Notebook 06 should run and be explored through these, not a parallel store. |

### 1.2 Notebook 06 work (uncommitted) — reuse, duplicates, replace

| Item | Assessment |
|---|---|
| `SigmaConfig.time_policy`, `elapsed_variance_v1` math, zero-return decay, `sqrt(dt)` jump scaling, explicit reset of the new fields | **Reuse.** Correct place (the estimator). Promote through strategy config (§3.1). The debug-only fields (`_last_return`, `_last_dt_s`, `_previous_variance`) become part of a public `explain()` view (§2.4). |
| `tests/unit/research/test_extreme_cases.py` estimator invariants (units across irregular intervals, half-life in seconds, zero-return decay, legacy unchanged, jump scaling, no synthetic samples, reconnect keeps policy) | **Reuse**, move to `tests/unit/indicators/` and extend. |
| `gap_reason` (point probability vs valid two-sided UP mid, invalid inputs → block) | **Reuse the rule**, re-implemented once in the driver (§3.2). |
| `StudyIndicatorGraph` (monkeypatches estimator configs, re-applies after reconnect) | **Duplicate / replace.** Unnecessary once the policy is an indicator parameter. |
| `ProbabilityStudy.before` calling the private `driver._observe` before `evaluate` | **Duplicate / replace.** Computes the probability twice and forces `entry_allowed=False` (reported as `BLOCKED`, a host capability), instead of a strategy `WAIT`. |
| `ProbabilityStudy.record` (`probability_explanation` with private estimator state) | **Replace** with strategy- and indicator-declared explain fields in the recording framework. |
| `research_study` hook in `ReplayAgent` / `run_window` | **Replace.** A replay-only extension point; the same behaviour must come from config. Remove after migration. |
| `replay_cases` own manifest, JSON bundles per run, `saved_results`, `latest_case_run`, `previous_comparison` (hard-coded notebook 05 path, config inference from `tau_max_s`) | **Replace** with a case-set run on `batch.run_batch` + `v3` identity + explorer loader (§4). Keep a read-only adapter for the saved bundles. |
| `RESEARCH_CASES` / `LIVE_CASES` inventories with purposes; `case_inventory`, `samples`, `case_metrics`, `entry_shadow`, `plot_case` over the saved audit | **Reuse** as a versioned case set and an evidence-only loader. |
| Saved evidence: `var/research/settlement_extreme_cases/development_cases_v1-{51563af1bc8c, da66a2810505, f49c304c2467}`, `var/research/probability_audit_20261019/`, `reuse_proof.json` | **Preserve read-only** (checksummed, §5.1). |
| `scripts/compare_twap_channels.py`, `twap_channel_comparison.md` and test | Out of scope; keep as is. |

### 1.3 Defects and gaps found during the review

These affect faithfulness and must be addressed regardless of the new features:

1. **Replay final gate omits feature readiness.** Live sets `final_gate.features_ready` (`trading_runtime.py:425-450`) and refuses with `EWMA_NOT_READY`; replay `_gate` (`strategy_replay/run.py:508-528`) builds `DispatchFacts` without it, so it never refuses for that reason. Live and replay differ today.
2. **Config hash hazard.** `settlement_value_config_as_parameters` omits only whole lifecycle sections at their defaults. Any new field in `volatility` or `entry` would change every `config_hash` / `config_id`, orphaning v2/v3 candidate ids and `timelines.replay_check`.
3. **Timelines regenerate unfaithfully.** `timelines._replay` calls `lab.run` without `execution_policy`, so `reference_availability_v1` or feature runs cannot be regenerated exactly.
4. **Explorer only loads staged experiments.** `load_experiment` needs `stage_*/stage.json`; v3 `Verification` / `FeatureRun` and notebook 06 outputs are not selectable.
5. **Three decision-row shapes.** Live `STRATEGY_DECISION` (`trading_runtime.py:1361`) builds its own dict; replay/observation use `settlement_decision_record_v1`; batch uses hard-coded `DECISION_COLUMNS`. No plugin hook declares a record schema.
6. **Run evidence is unbounded and unversioned.** Unbounded queue and `failures` list; `snapshot_and_close` reloads the whole run; `run_summary.json` embeds every record; `run_events` has no schema version, and `observed_at` is the write time, not the capture time.
7. **No raw-input capture in live or observation.** `RawWriter` is isolated in the collector, so no live run can be replayed. The two live losses cannot be reproduced for that reason.
8. **Per-input timing is not recorded with live decisions** (receipt/source times, clock snapshot, freshness ages per input).
9. **Synchronous I/O on the event loop** in several writers (`windows.jsonl`, `seal_*.json`, observation flush, execution journal FULL sync, ledger fsync). Essential writers stay synchronous by design (durability before POST); optional ones must not.

---

## 2. Recording as a framework feature

### 2.1 Evidence classes

| Class | Content | Durability | Optional? |
|---|---|---|---|
| **E — Essential** | Execution journal, settlement ledger, run summary | As today: synchronous, fsync/FULL, deduplicated, never dropped | Never |
| **O — Operational** | Current `run_events` types (readiness, outcomes, account updates, feed and loop diagnostics, window summaries) | Bounded background writer; counted drops | Always on, compact |
| **D — Decision diagnostics** | One record per evaluation: strategy-declared decision fields, host input timing | Bounded background writer; counted drops and gaps | By level |
| **X — Explanations** | Calculated state behind a decision: indicator internals (variance, last return, dt, policy), probability terms, side candidates | Same as D | By level |
| **R — Raw inputs** | Feed messages as received (Binance trades, RTDS TWAP incl. history, CLOB book events, account/user-stream messages) in warehouse `RawEnvelope` format | `RawWriter` (bounded bytes, group fsync, sealed segments) | Separate switch |

### 2.2 Recording levels (proposed)

| Level | Writes | Intended use |
|---|---|---|
| `essential` | E + O (operational events without `decision_evidence` rows) | Long unattended runs |
| `decisions` (**proposed default for live and observation**) | + D: one compact decision record per evaluation | Normal runs; replaces today's embedded `decision_row` |
| `explain` | + X for every evaluation, or only for evaluations matching a trigger (see below) | Investigating behaviour; notebook timelines |

`raw_inputs` is orthogonal (`off` / `on`) so a run can capture replayable inputs without
heavy explanations. Proposed trigger options for `explain`: `all`,
`entry_band` (evaluations inside the strategy's entry band), `actionable` (ENTER, veto,
hold, exit and their preceding N evaluations). Replay uses the same levels; batch maps
`keep_decisions` / `keep_timelines` onto `decisions` / `explain`.

### 2.3 Proposed configuration (run config, all modes)

```yaml
# PROPOSED — not implemented
recording:
  level: decisions            # essential | decisions | explain
  explain_trigger: entry_band # all | entry_band | actionable
  raw_inputs: off             # off | on  (warehouse RawEnvelope capture, replayable)
  directory: null             # default: <report run folder>/recording
  queue_max_records: 50000    # per stream (D, X)
  queue_max_bytes_mib: 64     # per stream
  segment_max_mib: 64
  segment_max_seconds: 300
  overflow: drop_newest       # D/X: drop_newest | drop_oldest ; never applies to E
  raw_on_fault: stop_raw_continue_trading   # or stop_run (strict capture)
  retention_days: 30          # D/X/R only; E/O are never pruned automatically
```

### 2.4 Who declares what

- **Strategies** declare their decision record via a new optional plugin hook
  `StrategyPlugin.decision_record_schema` returning a `RecordSchema(name, version,
  fields: (name, type, unit, level))`. The driver keeps producing `row` once (it already
  does, `driver.py:533-566`); the host projects it onto the declared fields. For
  settlement_value the schema is today's `settlement_decision_record_v1` row, versioned
  `settlement_value_decision_v2` when fields change. Remove the duplicate row in
  `decision.evidence` once live consumers read the recorder instead.
- **Indicators** expose `explain() -> Mapping` returning their current calculated state
  (EWMA: policy, variance and its units, last return, last dt, previous variance,
  effective samples, last sample time/price, jump-guard state). It reads stored state;
  it never recomputes.
- **Host** adds the generic part, shared by live, observation and replay (a generalized
  `decision_records.base_record`): run id, mode, window, `at_ns` (decision clock),
  `snapshot_observed_at`, clock policy id, `clock_snapshot_id`, offset and uncertainty,
  and per input: source timestamp, raw and corrected receipt time, `connection_generation`,
  `delivery_kind`, freshness age at decision time. These come from `IngressMeta` and
  `DecisionSnapshot` already held at decision time.

No extra venue requests, no recomputation of signals: records are projections of objects
already produced by the evaluation.

### 2.5 Pipeline: capture, buffering, serialization, writing

```text
evaluation (event loop) ──capture──► bounded queue ──► writer thread ──► segment files
   builds row once          O(1): reference immutable      serialize (JSON or      rotate + seal
                            objects; no serialization      Arrow batch), compress  manifest per segment
```

1. **Capture** (on the event loop, synchronous, cheap): `Recorder.capture(stream, record)`
   stores a reference to an immutable mapping already built by the evaluation, plus
   `capture_monotonic_ns` and `capture_wall_utc`. No JSON, no I/O, no locks shared with
   trading paths.
2. **Buffering**: one bounded queue per stream (records and bytes). On overflow, apply
   `overflow` policy, increment `dropped`, and open/extend a **recording gap** interval
   (first and last dropped capture time). Never blocks the caller.
3. **Serialization** in a background thread: JSON lines for D/X (schema id per record), or
   Arrow record batches when the schema is columnar. Serialization errors count as
   `serialize_failed` with the record's capture time; the record is dropped, not retried.
4. **Disk writing** in the same background thread: append to an open segment; rotate by
   size or age; on rotation write `segment.manifest.json` (schema, record count, first/last
   capture time, sha256). `fsync` on rotation and at shutdown only (D/X are diagnostics).
   Write errors switch the stream to `failed`: further captures are counted as dropped,
   the run continues, and an O-class `RECORDING_FAILED` event is written.
5. **Raw inputs** reuse `RawWriter` (its own threads and byte bound). Its existing
   overflow behaviour (fault and stop) is correct for the collector; in a trading process
   the proposed default is `stop_raw_continue_trading` with a visible gap, never a
   blocked feed.

**Visible counters** (per stream): captured, written, dropped, serialize_failed,
queue high-water (records, bytes), write latency p50/p99, open gaps. Exposed in a
periodic O-class `RECORDING_STATUS` event, in `run_summary.json` and in `watch-run`.

**Run evidence fixes** (same phase): bound the `RunEvidenceRecorder` queue, add a schema
version and a capture timestamp to `run_events`, stop embedding every record in
`run_summary.json` (store counts and a pointer), and stop reloading the whole run at
shutdown.

### 2.6 Output layout, schemas, provenance, retention

```text
<report run folder>/recording/
  manifest.json              # run id, mode, level, trigger, raw_inputs, schemas+versions,
                             # code identity (source fingerprint), config digest, strategy
                             # record schema, indicator policies, clock policy, start/end
  decisions/part-000001.jsonl.zst (+ .manifest.json)
  explain/part-000001.jsonl.zst   (+ .manifest.json)
  raw/  -> warehouse layout (collection = run instance id), replayable by strategy_replay
  status.json                # final counters and gaps
```

Retention: `retention_days` applies to D/X/R only, via an explicit
`tyrex-pm recording prune` command (dry run by default). E and O are never pruned
automatically.

### 2.7 Measurement, not promises

A benchmark (`tyrex-pm bench recording`, proposed) runs the same recorded window(s)
through the replay engine, and a short observe run, at each level (`essential`,
`decisions`, `explain all`, `explain entry_band`, `decisions + raw`). It reports:

- evaluate time p50/p99 and event-loop lag p99 (existing `EventLoopLagMonitor`);
- decision-to-submission latency from `ENTRY_OUTCOME.latency` (signal to submit start,
  prepare, final gate, POST) in paper mode;
- process RSS and queue high-water;
- records/s and bytes/s per stream, write latency, drops.

Proposed acceptance (to confirm after the first measurement): at `decisions`, no
measurable change in decision-to-submission p50 and < 1 ms added evaluate p99; at
`explain entry_band`, < 5 ms added evaluate p99; zero drops at nominal load; essential
evidence identical across levels.

---

## 3. Volatility and veto through shared strategy configuration

### 3.1 Versioned volatility time policy

**Naming (recommended):** `volatility.variance_time_policy` with values
`per_sample_v0` (today's formula, default) and `elapsed_variance_v1` (notebook 06
correction). The existing evidence's `"legacy"` maps to `per_sample_v0` in provenance.
An explicit versioned policy, not a boolean such as `enhanced`.

```yaml
# PROPOSED — strategy parameters, identical in live, observation and replay
strategy:
  parameters:
    volatility:
      half_life_s: 30.0
      variance_time_policy: per_sample_v0   # elapsed_variance_v1 = research only until qualified
```

Path (no research subclass, no monkeypatching):

1. `SettlementVolatilityConfig.variance_time_policy` + validation.
2. `ewma_vol_spec` emits `("variance_time_policy", …)` **only when not the default**, so
   existing indicator `instance_id`s stay unchanged.
3. `graph._EWMA_PARAM_KEYS` accepts it as an optional key; `sigma_config_from_spec`
   passes it to `SigmaConfig`. Reconnects rebuild through the same function, so the
   policy survives `invalidate_source` with no special handling.
4. `settlement_value_config_as_parameters` omits default-valued new fields, so every
   existing `config_hash` is preserved (fixes §1.3.2 for all future fields).
5. Add the key to `staged.EDITABLE["volatility"]`.
6. The decision record and the indicator `explain()` carry the policy.

### 3.2 Disagreement veto

```yaml
# PROPOSED
strategy:
  parameters:
    entry:
      disagreement_veto:
        enabled: false                # default: off
        max_gap_pp: 9.0               # exploratory hypothesis only; NOT an approved default
        probability: point            # point | conservative
        market_price: two_sided_mid   # midpoint of the same book snapshot the decision used
        invalid_inputs: wait          # wait (only option proposed)
```

- **Units:** percentage points of probability. `gap_pp = 100 × |P_model(UP) − mid(UP)|`
  where `mid(UP) = (best_bid_UP + best_ask_UP) / 2` from the same book snapshot as the
  candidates. Comparing UP with UP is equivalent to DOWN with DOWN only when both legs
  are consistent; the rule uses the UP leg as notebook 06 did. `probability: conservative`
  would compare the chosen side's conservative probability instead (unresolved, §6).
- **Invalid inputs** (missing or non-finite bid, ask or probability; `bid > ask`;
  outside `[0, 1]`; book not two-sided): `WAIT` with reason
  `DISAGREEMENT_INPUT_INVALID`.
- **Placement and timing:** in `driver.evaluate`, after the side is chosen and the
  re-entry wait has run, **before** `_start_episode` and the attempt counters
  (`driver.py:713-716`). Same snapshot, same evaluation, so the probability is computed
  once.
- **Effect:** `finish(StrategyAction.WAIT, R.DISAGREEMENT_VETO, model_ready=True)`. No
  episode starts, no attempt or POST is counted, no ledger reservation is made. This is a
  strategy `WAIT`, not a host `BLOCKED` capability. When it clears, the next evaluation
  re-checks every normal condition.
- **Scope:** entry only. It never runs in the position, hold, exit or pending branches,
  so it cannot change an existing hold or position.
- **Retries:** for a `REARMABLE` episode the veto also only waits; the episode's
  `max_retry_duration_s` keeps running (unresolved whether it should pause, §6).
- **Final gate:** not re-checked at dispatch in the first version (the gate stays
  quote-based); unresolved whether to add a gate-time re-check.
- The same driver runs in live and replay, and both parse the same config.

### 3.3 Live/replay parity fixes bundled with these features

- Replay `_gate` gets `features_ready` with the live rule (§1.3.1). This changes replay
  outcomes, so it ships as `replay_execution_contract_v2`, with `v1` kept for
  reproducing saved experiments (as `reference_availability="legacy"` does).
- `timelines._replay` forwards `execution_policy` and the full effective parameters.

---

## 4. Notebook 06 on the existing research tools

- **Case sets, not grids.** A `CaseSet(name, version, windows with purposes)` registry
  (initial: `development_cases_v1` = the seven replayable windows). A `CaseRun` is a
  `FeatureRun`-style saved run over a case set: `batch.run_batch` specs, resumable parts,
  `summaries.parquet` / `decisions.parquet`, `experiment.json`. Variants are **config
  overlays** (`volatility.variance_time_policy`, `entry.disagreement_veto`), not hooks.
- **Identity / provenance** in `experiment.json`: kind `case_run`, case set and version,
  candidate parameters (effective), variant overlay, scenario, execution policy (incl.
  `reference_availability`, replay contract version), recording level and trigger,
  feature policies derived from config, `replay_identity()`, `code_identity()`, data
  cache keys.
- **Explorer:** add a loader for v3-style directories (`Verification`, `FeatureRun`,
  `CaseRun`) so they appear in `available_datasets()` next to staged experiments, using
  `explorer.prepare` + `DatasetSpec`. Timelines then read `decisions.parquet` (and
  `explain/` when recorded) for any notebook 04/05/06 run.
- **Comparison tables:** reuse `v3.totals` / `feature_vs_baseline`; add a per-case table
  (case, candidate, variant, filled, side, entry τ, P&L, attempts, posts, vetoed
  evaluations from the reason column).
- **Previous results:** a read-only adapter loads the three
  `settlement_extreme_cases/development_cases_v1-*` bundle directories as datasets
  labelled `legacy_study_bundle`, so the 42 saved runs stay inspectable and comparable.
- **Live evidence cases** are a separate kind, `live_evidence`, loaded from the saved
  audit and run journals, flagged not replayable. Once raw capture exists (§2), new live
  or observation runs become replayable case collections automatically.

---

## 5. Migration, cleanup and verification

### 5.1 Phases

| Phase | Content | Depends on | Acceptance |
|---|---|---|---|
| **P0 Preserve** | Commit the notebook 06 work as is (separate commit, no behaviour change in defaults); checksum manifest of `var/research/settlement_extreme_cases/` and `probability_audit_20261019/` | — | Clean tree; existing tests pass; checksums recorded |
| **P1 Config plumbing** | Hash-stable serialization (omit default new fields); `variance_time_policy` through strategy config → spec → graph → estimator; move estimator tests | P0 | All existing `config_hash`/`config_id` and indicator `instance_id`s unchanged; legacy decision digests identical on the v3 regression window; live and replay build the same `SigmaConfig` from the same YAML |
| **P2 Veto** | `entry.disagreement_veto` in driver; reason codes; record fields | P1 | Disabled: digests identical; enabled: waits without attempts/posts/reservations; holds untouched; replay and live (harness) identical |
| **P3 Parity** | Replay gate `features_ready` as `replay_execution_contract_v2` (v1 kept); `timelines._replay` forwards policy | P1 | v1 reproduces saved v3 results exactly; v2 matches the live gate on scripted cases |
| **P4 Recording core** | `Recorder` pipeline, levels, schema hook, indicator `explain()`, run-evidence fixes, counters, `watch-run` display, benchmark | P1 | Recording tests (§5.3) pass; benchmark meets agreed thresholds; essential evidence unchanged across levels |
| **P5 Raw capture** | `raw_inputs` in observe and live via `RawWriter`; run-scoped warehouse collection; replay of a recorded observe run | P4 | An observe run replays through `strategy_replay` and reproduces its recorded decisions (within documented clock tolerance) |
| **P6 Research tooling** | `CaseSet`/`CaseRun`, explorer loader for v3 directories, legacy bundle adapter, notebook 06 rewired; then remove `StudyIndicatorGraph`, `ProbabilityStudy`, `research_study` hook | P1–P3 | Migration equivalence (below); notebooks 04/05 unchanged; Run All on 06 reads evidence only |
| **P7 Research qualification** | Frozen hypothesis on the 94 development windows, then untouched windows; recorded observation | P6 | Separate review; no live change implied |
| **P8 Live enablement** | Template flags, observe with `explain` + `raw_inputs`, supervised trial | P7 | Separate approval |

**Migration equivalence (P6):** rerun the 42 case runs through the config path and
compare to the saved bundles. `unchanged` and `time_corrected` must match decision
digests exactly. The veto variant must match actions, intents, fills and economics;
reason strings differ by design (`BLOCKED` research gate vs strategy `WAIT`), so the
comparison maps the old study reason onto the new one. Only after this passes are the
temporary classes and the replay hook removed.

### 5.2 Ordering rationale

Config plumbing first (everything else depends on hash-stable parameters); the veto and
parity fixes are small and independent; recording comes before raw capture; notebook
migration last, so it can rely on the shared paths and be checked against preserved
results.

### 5.3 Tests

| Requirement | Tests |
|---|---|
| Identical legacy behaviour with features off | `config_hash`/`config_id` and indicator ids unchanged for all candidate files and templates; decision digests unchanged on the recorded regression window (`test_replay_v3_mechanics`); live harness outcomes unchanged |
| Shared config and calculation | Same YAML parsed by run config and by the research path produces equal `SettlementValueConfig` and `SigmaConfig`; estimator invariants (units, half-life seconds, zero-return decay, jump scaling, no synthetic samples, floor, reset); policy survives `invalidate_source` |
| Corrected clock, freshness, reconnects, arrival times | Veto uses the strategy's corrected snapshot time (the notebook 06 offset test, moved); recorded input timings equal the `IngressMeta` values; reconnect gap and history recovery keep actual arrival times; stale tape still blocks |
| Veto waits without consuming attempts | No episode/attempt/post/reservation while vetoed; entry proceeds when cleared with all normal checks; REARMABLE episodes only wait; positions and holds unaffected; invalid inputs wait |
| Recording correctness and bounded resources | Records match decisions 1:1 at `decisions`; overflow drops and gap intervals counted; queue never exceeds bounds; serialize and write failures do not stop trading and are reported; segments and manifests valid; essential evidence identical across levels; shutdown flushes and seals |
| Explorer compatibility | v3 and case-run directories listed and loaded by `available_datasets` / `load_dataset`; timelines read their decisions; legacy bundle adapter loads the 42 saved runs |

---

## 6. Recommended defaults and unresolved decisions

### Recommended defaults

| Setting | Default | Note |
|---|---|---|
| `volatility.variance_time_policy` | `per_sample_v0` | Current behaviour |
| `entry.disagreement_veto.enabled` | `false` | 9 pp is an exploratory hypothesis only |
| `recording.level` (live, observe) | `decisions` | Replaces today's embedded decision rows |
| `recording.level` (replay batch) | `decisions` when `keep_decisions`, else `essential` | Matches current toggles |
| `recording.raw_inputs` | `off` live, `on` for observe runs used as research evidence | Needs a disk budget decision |
| Replay execution contract | `v1` for existing experiments, `v2` for new | Parity fix |

### Unresolved decisions

1. Policy name: `variance_time_policy: per_sample_v0 | elapsed_variance_v1` vs another scheme.
2. Veto comparison: point probability vs conservative bound; UP leg vs chosen side.
3. Whether the veto also re-checks at the final gate with the freshest book.
4. Whether `max_retry_duration_s` pauses while a REARMABLE episode is vetoed.
5. `explain` trigger default (`entry_band` vs `actionable`).
6. Raw capture in live runs: disk budget, retention, and behaviour on capture fault (`stop_raw_continue_trading` vs `stop_run`).
7. Benchmark thresholds (§2.7) after the first measurement.
8. Whether the replay gate parity fix should be applied retroactively to research comparisons (new experiments only is proposed).

### Benchmarks to run before P4 acceptance

At each recording level, on the same recorded windows and a short observe run: evaluate
p50/p99, event-loop lag p99, decision-to-submission p50/p99 (paper), RSS, records/s,
bytes/s, write latency, drops. Report recording-on vs off differences with their
run-to-run variability; no zero-overhead claim.
