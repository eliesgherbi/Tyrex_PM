# Reporting design recommendation

**Status:** design recommendation only (not an implementation plan; not authorized to implement)  
**Depends on:** `current_reporting_audit.md`  
**Repository HEAD verified at audit:** `rest_project` @ `18235c84c99bb0066bcc5271bd761fb191b0548c`

Configuration and CLI behavior for OBSERVE/SHADOW/LIVE are **accepted and out of scope** to redesign. This document proposes a reporting contract and profiles that can attach to the existing YAML `run` workflow later.

---

## 1. Recommended conceptual architecture

### 1.1 Separate three channels (always)

| Channel | Purpose | Default retention | May be disabled? |
|---|---|---|---|
| **A. Operational / safety** | preflight, auth arming, seals/PTB, mutations, orders/fills, recon, residuals, terminal outcome, reporting-health | Always on for LIVE; always on for any mode that touches OMS | **No** for safety-critical event classes |
| **B. Strategy / research analytics** | evaluation/decision records, gate outcomes, distributions, closest candidates | On for `analysis` profile; sampled/aggregated under `operational` | Configurable **except** when an intent is emitted (then lossless decision context required) |
| **C. Debug / high-volume trace** | raw feeds, unchanged WAIT spam, transport dumps | Off by default; `debug` profile or separate raw recorder | Yes |

Do **not** mix A/B/C into one undifferentiated JSONL by default.

### 1.2 Cross-strategy recording model

```text
stable envelope (strategy-agnostic)
  + typed stage body (common fields per stage)
  + strategy_diagnostics{ strategy_id, schema, version, payload }
```

Producers emit through a thin **RecordingPort** (name illustrative) owned by runtime/reporting — **not** `if strategy == "z_gap"` switches in a central reporter. Strategies/bindings contribute:

- common fields they already know (`action`, `reason_code`, …), and  
- an optional **namespaced** diagnostics blob (Z-Gap today: calibration/gates).

Hosts remain responsible for mode legality (OBSERVE cannot invent fills).

### 1.3 Reuse what already exists

- Keep `FactEnvelope` + JSONL as the append-only bus for streaming records.  
- Elevate `z_gap_calibration_v1` / `StrategyDecision` as the template for Z-Gap diagnostics (already computed in `ZGapBinding`).  
- Keep N7 `n7_oneshot_report.json` as the LIVE operator summary; **extend** it with analytical summary blocks rather than replacing safety fields.  
- Fix the LIVE gap by persisting values already returned from `evaluate()` (no recomputation in the reporter).

### 1.4 Proposed per-run directory (evolutionary)

Compatible with current `var/reporting/yaml_run/<run_name>/` and N7 trees:

```text
var/reporting/yaml_run/<run_name>/
  manifest.json                 # NEW (small)
  summary.json                  # NEW or evolved oneshot report
  events.jsonl                  # NEW unified stream (optional profiles)
  operational/                  # optional split
    preflight/…
    seals/…
  analytics/                    # optional
    decisions.jsonl             # or subset of events
    gate_histogram.json
  debug/                        # optional, profile-gated
    host_trace.jsonl
  # Compatibility (retain initially):
  n7_oneshot_report.json        # LIVE
  compose/…                     # LIVE (may slim observations later)
  observe_facts.jsonl / shadow_facts.jsonl   # transitional
```

Legacy `var/reporting/n7/oneshot_*` remains valid via the same writers.

---

## 2. Analysis questions → contract coverage

| Question class | Primary artifact | Notes |
|---|---|---|
| Run-level identity/health | `manifest.json` + `summary.json` | code SHA, mode, source, strategy/risk/exec/runtime paths, scenario, sealed K, completeness, drop counts |
| Decision-level why | `decision` records in `events.jsonl` | action, primary_reason, gates[], inputs, thresholds, proximity |
| Execution-level | `order`/`fill`/`plan`/`risk` records | lossless for real/sim mutations |
| Position/exit | `position`/`exit`/`flatten`/`recon` | theoretical vs executable vs realized labels |
| Cross-run | manifests + summaries + optional index | no DB required; JSONL/Parquet optional later |

---

## 3. Proposed schemas (contracts)

Conventions:

- Timestamps: UTC ISO-8601.  
- Decimals: strings.  
- `schema`: `<name>_v<N>`.  
- Envelope fields common to streaming records below.

### 3.0 Stream envelope (all JSONL events)

```json
{
  "schema": "run_event_v1",
  "ts": "2026-07-24T22:45:03.120Z",
  "run_id": "…",
  "mode": "live",
  "source": "live",
  "strategy_id": "z_gap",
  "market_id": "0x…",
  "window_id": "btc-updown-5m-…",
  "correlation_id": "…",
  "seq": 42,
  "stage": "decision",
  "body": { },
  "strategy_diagnostics": {
    "schema": "z_gap_decision_diag_v1",
    "payload": { }
  }
}
```

| | |
|---|---|
| Mandatory | `schema,ts,run_id,mode,stage,seq,body` |
| Optional | strategy/market/window, diagnostics, causation |
| Producer | host recording adapter |
| Trigger | stage emission |
| Cardinality | many / run |
| Lossless? | depends on stage (see below) |
| Analysis | join key for all stages |

### 3.1 `run_manifest_v1`

```json
{
  "schema": "run_manifest_v1",
  "run_id": "yaml_live_20260724T224238Z",
  "requested_mode": "live",
  "effective_mode": "live",
  "data_source": "live",
  "live_armed": true,
  "fake_rehearsal": false,
  "git_head": "18235c84…",
  "worktree_clean": false,
  "strategy_path": "config/strategies/z_gap.yaml",
  "risk_path": "config/risk/tiny_live_5usd.yaml",
  "execution_path": "config/execution/polymarket_live.yaml",
  "runtime_path": "config/runtime/live_btc_5m.yaml",
  "scenario": null,
  "reporting_profile": "analysis",
  "reporting_schema_versions": {"run_event": 1, "summary": 1},
  "host_binding": "n7_operator_oneshot",
  "started_at": "…",
  "ended_at": null,
  "partial": false
}
```

| | |
|---|---|
| Producer | YAML/N7 run entry |
| Trigger | run start (+ finalize end fields) |
| Cardinality | 1 |
| Lossless | yes |
| Use | cross-run identity |

### 3.2 Operational / safety events (`stage`: `operational`)

Body examples: `preflight_result`, `live_arm`, `ptb_seal`, `mutation_boundary`, `kill_state`, `connectivity`.

```json
{
  "event": "ptb_seal",
  "sealed_k": "64122.086130005",
  "ptb_authority": "chainlink_sealed_k",
  "ssr_check_status": "DISABLED",
  "ptb_ready": true,
  "boundary_rule": "EXACT_AT_START"
}
```

| | |
|---|---|
| Lossless | **yes** (critical) |
| Sampling | never for seal/arm/mutation/recon failures |
| Use | operator trust + audit |

### 3.3 Strategy evaluation / decision (`stage`: `decision`)

Common body (strategy-agnostic):

```json
{
  "action": "SKIP",
  "primary_reason": "Z_OUT_OF_BAND",
  "reason_codes_all": ["Z_OUT_OF_BAND"],
  "gates": [
    {"code": "TAU_IN_BAND", "passed": true, "value": 180.0, "threshold": {"min": 60, "max": 210}},
    {"code": "Z_IN_BAND", "passed": false, "value": 0.41, "threshold": {"min": 0.8, "max": 2.2}}
  ],
  "eligible_for_entry_gates": true,
  "intent_emitted": false,
  "proximity": {"metric": "abs_z_to_band", "distance": 0.39}
}
```

Allowed `action` values (already in framework):  
`WAIT | SKIP | ENTER | HOLD | EXIT | FLATTEN | BLOCKED`.

Z-Gap diagnostics payload (namespaced):

```json
{
  "schema": "z_gap_decision_diag_v1",
  "tau_s": 180.0,
  "z": 0.41,
  "sigma": 0.00012,
  "K": "64122.086130005",
  "S": "64130.12",
  "p_up": "0.51",
  "p_down": "0.49",
  "up_ask": "0.52",
  "up_bid": "0.50",
  "down_ask": "0.50",
  "down_bid": "0.48",
  "up_e_repricing": "0.02",
  "down_e_repricing": "-0.01",
  "selected_leg": null,
  "fee_rate": "0.07",
  "theta_take": "0.05",
  "z_min": "0.8",
  "z_max": "2.2"
}
```

| | |
|---|---|
| Producer | binding → recording adapter (reuse calibration + decision) |
| Trigger | each evaluation that reaches strategy decision **or** sampled subset |
| Cardinality | ≤ evaluations |
| Lossless | **yes** if intent emitted OR safety block; otherwise profile-dependent |
| Use | why enter/not enter; gate histograms |

**Gate semantics (critical):**

- `primary_reason`: first decisive reject/accept code from the strategy’s actual control flow (Z-Gap: readiness then selection).  
- `gates[]`: only gates **evaluated** on that path (do not invent failures for skipped later gates).  
- Aggregates must count: `evaluations`, `eligible_for_entry_gates`, `primary_reason` histogram, and optionally co-failure among evaluated gates — never treat “all possible gates” as independent Bernoulli trials.

### 3.4 Intent + risk (`stage`: `intent` / `risk`)

```json
{"intent_type": "EnterIntent", "intent_id": "…", "target_notional": "5", "outcome_side": "YES"}
```
```json
{"decision": "ALLOW|DENY", "reasons": ["SPREAD_TOO_WIDE"], "authorized_notional": null}
```

Lossless for all intents and risk denies affecting live/SHADOw paths.

### 3.5 Execution plan / order / fill (`stage`: `plan` / `order` / `fill`)

Lossless whenever an OMS exists (SHADOW or LIVE). Include intended price/qty, submitted, ack/ambiguous, cancel, fill price/qty, fee labels (`estimated` vs confirmed).

### 3.6 Position / lifecycle / exit / flatten (`stage`: `position` / `exit` / `flatten` / `recon`)

Lossless transitions. Include reason codes (`MARKET_RICH_EXIT`, time flatten, kill, etc.), inventory before/after, residual flags.

### 3.7 Final run summary (`run_summary_v1`)

Human + machine top-level (evolves today’s oneshot report):

```json
{
  "schema": "run_summary_v1",
  "outcome": "PASS_N7_SAFE_NO_ENTRY",
  "reason": "evaluated_no_enter_signal",
  "counts": {
    "evaluations": 159,
    "decisions_recorded": 159,
    "intents": 0,
    "risk_denies": 0,
    "orders": 0,
    "fills": 0,
    "real_venue_mutations": 0
  },
  "analytics": {
    "primary_reason_histogram": {"Z_OUT_OF_BAND": 90, "BELOW_THRESHOLD": 40, "TAU_OUT_OF_BAND": 29},
    "eligible_for_entry_gates": 130,
    "closest_candidate": {
      "ts": "…",
      "primary_reason": "BELOW_THRESHOLD",
      "proximity": {"metric": "edge_gap", "distance": "0.008"},
      "strategy_diagnostics_ref": {"seq": 118}
    }
  },
  "ptb": {"sealed_k": "64122.086130005", "ptb_authority": "chainlink_sealed_k"},
  "reporting_health": {"events_written": 200, "events_dropped": 0, "partial": false}
}
```

| | |
|---|---|
| Cardinality | 1 |
| Lossless | yes for ops fields; analytics blocks derived from recorded decisions |
| Use | first file an operator opens |

### 3.8 Reporting-health metadata

Counters: written, dropped_by_sampling, dropped_by_backpressure, flush_errors, partial_run.  
Must appear in summary even when analytics sampling is aggressive.

---

## 4. Noise-control semantics

### 4.1 Always lossless

- All safety/ops events (preflight, arming, seal, kill, mutation boundary).  
- Every intent, risk decision on an intent, plan, order, fill, cancel, ambiguous submission.  
- Every exit/flatten/recon/residual.  
- Any evaluation that emits an intent or `BLOCKED` for safety.  
- Reporting-health.

### 4.2 Configurable for no-action evaluations

For `WAIT`/`SKIP` without intents:

| Mechanism | Role |
|---|---|
| Record decision transitions only | emit when `primary_reason` or `action` changes |
| Periodic heartbeat sample | e.g. every N seconds / every M evals |
| Aggregate histogram | finalize in summary |
| Closest-K candidates | retain K best proximity scores |
| First/last per window | anchors |
| Full decision stream | `analysis` / `debug` profiles |

### 4.3 Illustrative no-entry summary (semantics, not invented counts)

```text
evaluations: 159
eligible_for_entry_gates: <count reaching leg selection>
primary_reason_histogram:
  TAU_OUT_OF_BAND: …
  Z_OUT_OF_BAND: …
  BELOW_THRESHOLD: …
  …
closest_candidate: { … }
```

Exact histogram keys must come from persisted `primary_reason` values (Z-Gap `ZGapReason`), not guessed.

### 4.4 Raw market data

Remain a **separate** optional recorder (N1-style), never the default trading report channel.

---

## 5. Configurable reporting proposal

### 5.1 Attachment point

**Recommendation:** add an optional fifth YAML section / CLI flag later:

```bash
python -m tyrex_pm.application.cli run ... \
  --reporting config/reporting/analysis.yaml
```

Rationale:

- Reporting concerns are cross-cutting (not strategy math, not risk caps, not OMS policy).  
- Keeps accepted strategy/risk/execution/runtime ownership intact.  
- Defaults can be mode-sensitive when flag omitted (`live` → operational+analytics summary; observe/shadow → current facts compatibility).

Alternative (inferior): bury knobs in `runtime/*.yaml` — couples data-source config with sink policy.

**Not authorized to implement the flag in this task.**

### 5.2 Profiles (proposed names)

| Profile | Intent |
|---|---|
| `operational` | Safety/ops + summary histograms; sample no-action decisions |
| `analysis` | Default for research: full decision diagnostics (or dense sampling) + ops lossless |
| `debug` | analysis + host trace fan-out + optional raw pointers |

### 5.3 Example profile contents (illustrative only — do not create files now)

`operational.yaml` (example text):

```yaml
profile: operational
channels:
  operational: true
  analytics: true
  debug: false
analytics:
  decision_level: summary_plus_transitions  # transitions|sampled|full
  sample_every_n: 25
  keep_closest_candidates: 5
  emit_gate_histogram: true
outputs:
  manifest: true
  summary: true
  events_jsonl: true
  compatibility_host_facts: true   # keep observe/shadow JSONL during migration
flush:
  mode: batched
  max_batch: 32
  max_buffer_ms: 50
safety:
  allow_disable_operational: false
redaction:
  secrets: true
  shorten_addresses: true
```

`analysis.yaml` (example text):

```yaml
profile: analysis
channels: { operational: true, analytics: true, debug: false }
analytics:
  decision_level: full
  keep_closest_candidates: 20
  emit_gate_histogram: true
outputs:
  manifest: true
  summary: true
  events_jsonl: true
  compatibility_host_facts: true
flush: { mode: batched, max_batch: 16, max_buffer_ms: 25 }
```

`debug.yaml` (example text):

```yaml
profile: debug
channels: { operational: true, analytics: true, debug: true }
analytics: { decision_level: full }
debug:
  host_trace_facts: true          # timer/freshness/indicator fan-out
  raw_market_data: false          # still explicit opt-in
flush: { mode: sync }             # strongest durability for short probes
```

### 5.4 Option matrix (selected)

| Option | Type / default | Controls | Mandatory? | Modes | Perf/storage | Safety |
|---|---|---|---|---|---|---|
| `channels.operational` | bool / true | ops events | effectively yes | all | low | cannot disable critical subclasses |
| `channels.analytics` | bool / true | decision records | no | all | med–high | intents still force context |
| `channels.debug` | bool / false | host trace | no | all | high | n/a |
| `analytics.decision_level` | enum / mode-dependent | density of WAIT/SKIP | no | all | dominant knob | n/a |
| `sample_every_n` | int / 0 | periodic samples | no | all | reduces JSONL | n/a |
| `keep_closest_candidates` | int / 5 | proximity retain | no | all | tiny | n/a |
| `emit_gate_histogram` | bool / true | summary aggregates | no | all | tiny | n/a |
| `outputs.compatibility_host_facts` | bool / true initially | old JSONL | migration | observe/shadow | high | n/a |
| `flush.mode` | sync\|batched / batched | durability vs hot path | no | all | latency | batched must flush on shutdown/intent |
| `redaction.*` | bool / true | secret hygiene | yes defaults | all | neg. | required |
| `raw_market_data` | bool / false | separate raw recorder | no | live/observe | very high | never substitutes ops |

---

## 6. Performance and failure-handling contract

### 6.1 Principles

1. Reporter **never recomputes** Z-Gap mathematics; it records existing `StrategyEvalResult` / OMS reports.  
2. Avoid sync-per-tick `flush()` as default (today’s `JsonlFactSink` risk). Prefer bounded batching with **deterministic final flush**.  
3. Bound memory with a fixed queue; on overflow: **never drop** lossless classes; drop/sample only eligible no-action analytics; increment `events_dropped_*`.  
4. Intent/order/fill emission triggers immediate flush of pending batches.  
5. Partial runs: `manifest.partial=true` + whatever JSONL was flushed remains readable.  
6. Ordering: monotonic `seq` per run; tests can assert order for fixture hosts.

### 6.2 Measurable acceptance criteria (evidence-based, relative)

Current evidence: fixture OBSERVE emits ~8–11 JSONL lines/eval with sync flush; LIVE ran **159** evals in ~4.5 minutes post-seal (~0.6 Hz), while feeds were far denser (thousands of ticks). Reporting is not the feed bottleneck today, but sync multi-fact writes will not scale cleanly to denser decision loops.

Propose future gates (to be measured in implementation phase against fixture + recorded live replay):

| Criterion | Proposal |
|---|---|
| Hot-path overhead | p95 added time in host `_emit`/record path ≤ **5%** of median evaluate() time on fixture OBSERVE, or ≤ **0.5 ms/eval** median on the same hardware used for acceptance — pick the looser bound only after measuring baseline `evaluate()` |
| Queue | max buffered records ≤ configurable (e.g. 1024); overflow behavior tested |
| Lossless integrity | 0 drops for ops/intent/order/fill in tests |
| Shutdown | all pending records flushed before process exit code decided |
| LIVE analytics | for a 159-eval no-entry run under `analysis`, summary must include primary_reason histogram + closest candidate; full decision stream size bounded/tested |

Do **not** invent a hard microsecond SLA without a harness; establish baselines first.

---

## 7. Storage and usability

| Format | Role |
|---|---|
| **JSON** | manifest, summary, seals, preflight (human + git-diff friendly) |
| **JSONL** | event stream (Python/pandas friendly) |
| CSV | optional export later; not primary |
| Parquet | **not recommended now** (no dependency justification; adds stack surface) |
| Database | **out of scope / non-goal** |

**Minimum default per-run set:**

1. `manifest.json`  
2. `summary.json` (ops + analytics digest)  
3. `events.jsonl` (at least lossless classes; decisions per profile)  
4. LIVE compatibility: retain `n7_oneshot_report.json` until readers migrate  

Optional: `debug/`, raw capture dir, compatibility host facts.

**Index across runs (lightweight, optional later):**  
`var/reporting/yaml_run/index.jsonl` append-only one line per run (run_id, paths, outcome, mode) — not a DB.

Humans open `summary.json` first; notebooks read `events.jsonl`.

---

## 8. Compatibility and migration (high level only)

1. **Keep writing** existing N7 oneshot reports, preflight trees, seals, and OBSERVE/SHADOW host JSONL initially (`compatibility_host_facts: true`).  
2. **Add** manifest/summary/events alongside (non-breaking).  
3. Wire LIVE `_capture_intents` (or adjacent adapter) to emit decision records from existing `StrategyEvalResult` — behavioral trading semantics unchanged.  
4. Slim compose `observations[]` stubs once events.jsonl exists (compatibility flag).  
5. Introduce `--reporting` profiles after contracts stabilize.  
6. Normalize readers/tools gradually; do not rewrite historical `var/` files.  
7. Retire duplicate fields only when consumers confirmed.

Risks: tests that snapshot exact compose observation shape; tools grepping oneshot fields (keep aliases).  
Schema version fields prevent silent drift.

---

## 9. Mapping to the armed no-entry run (what would change)

With `analysis` profile applied to the same 159-eval LIVE run, an operator should open `summary.json` and immediately see:

- sealed K / PTB authority (unchanged ops proof),  
- `evaluations: 159`, `intents: 0`, `real_venue_mutations: 0`,  
- **primary_reason_histogram**,  
- **closest_candidate** with z/edge/τ and threshold gaps,  
- pointer into `events.jsonl` for full rows.

No need to mine 159 identical `"StrategyDecision"` stubs.

---

## 10. Decisions requiring approval

1. **Fifth YAML `--reporting` section** vs runtime-owned knobs.  
2. **Default profile by mode** (`live` → `analysis` vs `operational`).  
3. Whether LIVE must emit **full** decision JSONL by default or histogram+closest under `operational`.  
4. How long to keep dual-writing host `*_facts.jsonl` + new `events.jsonl`.  
5. Whether compose `observations` may be slimmed once events exist.  
6. Batching defaults vs sync flush for LIVE.  
7. Retention/redaction policy for addresses in shared artifacts.  
8. Scope of closest-candidate metric definition (edge gap vs |z| band distance vs combined score).

---

## 11. Explicit non-goals (later implementation must not expand into)

- Redesigning OBSERVE/SHADOW/LIVE CLI semantics or N7 safety model.  
- Second live engine.  
- Database, dashboard, experiment tracker, or cloud service.  
- Recomputing strategy math inside reporters.  
- Forcing trades / threshold changes to generate entries.  
- Deleting or rewriting historical `var/` evidence.  
- Raw tick storage as the default analytical product.  
- Hard-coded central `if strategy == "z_gap"` field lists for all future strategies (diagnostics must be namespaced).  
- This document is **not** an implementation plan (no work packages / schedule).

---

## 12. Conclusions (required)

1. **Architecture:** three channels (ops/analytics/debug); envelope + stage body + namespaced strategy diagnostics; reuse `evaluate()` outputs; evolve N7 summary rather than replace safety evidence.  
2. **Mandatory vs configurable:** ops/mutation/recon/intent/order/fill/reporting-health mandatory; WAIT/SKIP density and debug/raw configurable.  
3. **Minimum default artifacts:** `manifest.json`, `summary.json`, `events.jsonl` (+ transitional oneshot/host facts).  
4. **Cross-strategy contracts:** `run_event_v1` stages for operational, decision, intent, risk, plan, order, fill, position/exit, plus `run_manifest_v1` / `run_summary_v1`.  
5. **Profiles:** `operational`, `analysis`, `debug` via proposed `--reporting` YAML.  
6. **Noise control:** lossless critical path; transition/sample/aggregate no-action evals; primary_reason vs evaluated-gates semantics; closest-K.  
7. **Performance/failure:** no recompute; batched flush with intent-triggered flush; bounded queue; drop counters; partial-run flags; relative overhead acceptance after baseline measurement.  
8. **Compatibility:** dual-write first; keep N7 report fields; don’t rewrite old `var/`; version schemas.  
9. **Approvals needed:** §10 list.  
10. **Non-goals:** §11 list.

**Readiness statement:** The audit evidence is sufficient to justify implementing a reporting upgrade **after** approval of the decisions in §10. It is **not** claimed that implementation may begin without that review.
