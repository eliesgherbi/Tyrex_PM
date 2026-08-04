# Current reporting audit

**Status:** investigation only (no implementation)  
**Repository HEAD verified:** `rest_project` @ `18235c84c99bb0066bcc5271bd761fb191b0548c`  
**Audit date:** 2026-07-25  
**Authoritative sources:** current source under `src/tyrex_pm/`, tools under `tools/`, and the live `var/` tree (not modified).

---

## 1. How reporting works today (factual)

### 1.1 Conceptual spine

```text
feeds / fixtures
  → host composition (ObserveHost | ShadowHost | N4ObserveRuntime | live_zgap_compose | N7 session)
  → strategy binding.evaluate(...) → StrategyEvalResult
       includes StrategyDecision + intents + signal_payload + extra_facts
  → (OBSERVE/SHADOW hosts) JsonlFactSink.append(FactEnvelope)  → *.jsonl
  → (SHADOW) ShadowOMS / lifecycle facts into same JSONL
  → (N4/N5 tools) ObserveRecord / shadow records → summary JSON (+ optional facts JSONL)
  → (N7 / yaml --mode live) operator JSON report + compose/summary.json + preflight JSON
       N7 callback deliberately keeps only a thin eval stub (see §6.4)
```

There is **no unified analytical reporter**. Reporting is host- and stage-specific:

| Layer | Primary writer | Typical output |
|-------|----------------|----------------|
| Framework facts | `tyrex_pm.reporting.jsonl.JsonlFactSink` | append-only JSONL (`schema_version=1`) |
| OBSERVE / SHADOW hosts | `ObserveHost` / `ShadowHost` → `_emit` | `var/reporting/.../*_facts.jsonl` |
| Z-Gap analytics (when host emits) | `ZGapBinding.evaluate` → `extra_facts` | `zgap_model_snapshot`, `zgap_entry_valuation`, `zgap_calibration_row` |
| N4/N5 live composition | `live_zgap_compose` + `N4ObserveRuntime` | `compose/summary.json`, `seal_*.json` |
| N7 / yaml live | `n7_operator_run` / `yaml_config.live_run` | `n7_oneshot_report.json`, `effective_n7_sealed.json`, `preflight/*` |
| R6/R7 legacy live | `r7b_live_once`, CLI defaults | `var/reporting/r6|r7|r7b|...` |
| Tools N1–N5 | standalone scripts | `var/reporting/n1`…`n5` |
| Durable ops state | R7 ack / residuals / shadow snapshots | `var/state/**` (not disposable reports) |

### 1.2 Fact contract (when JSONL is used)

- Envelope: `tyrex_pm.core.facts.FactEnvelope` (`fact_type`, `schema_version`, `ts`, `run_id`, `correlation_id`, `payload`, optional `strategy_id` / `causation_id`).
- Sink: `JsonlFactSink` — UTF-8 JSONL, **flush after every append** by default (`flush_every=True`).
- Schema version constant: `JsonlFactSink.SCHEMA_VERSION = 1`.
- Writers generally **persist values already computed** by strategy/risk/OMS; they do not recompute Z-Gap math in the sink.

### 1.3 Mode differences (schemas are not unified)

| Mode | Decision analytics in artifacts? | Mutation evidence | Notes |
|------|----------------------------------|-------------------|-------|
| OBSERVE (YAML/`ObserveHost`) | **Yes** — full `extra_facts` + `observe_decision` | None by design | Best current analytical source for Z-Gap |
| SHADOW (YAML/`ShadowHost`) | **Yes** — same + OMS/lifecycle facts | Simulated only | Same calibration rows as OBSERVE |
| LIVE (`run --mode live` → N7) | **No per-eval analytics** in compose stubs | Real only if `--live` after preflight GO | Operationally rich; analytically starved |
| N4 tool observe | Partial `ObserveRecord` fields in summary | None | Richer than N7 stubs; thinner than host JSONL |
| N5 tool shadow | Evaluation + lifecycle JSONL/summary | Simulated | Includes `n5_shadow_evaluation` |
| R7b live-once | Facts JSONL + report JSON | Optional venue | Legacy path; different fact taxonomy |

### 1.4 `run_name` / directory resolution

- YAML OBSERVE/SHADOW: `yaml_config.adapt.output_path_for_run` → `var/reporting/yaml_run/<run_name>/<mode>_facts.jsonl`.
- YAML LIVE: `yaml_config.live_run.run_yaml_live` → `var/reporting/yaml_run/<run_name|yaml_live_<stamp>>/` containing N7-shaped artifacts (not host JSONL).
- N7 operator tool: `var/reporting/n7/oneshot_<stamp>/` by default.
- Path policy commentary: `tyrex_pm.runtime.r7_paths` — `var/reporting/**` disposable; `var/state/**` durable local state.

### 1.5 Hot-path / failure behavior

- OBSERVE/SHADOW: each evaluation emits multiple facts (timer, freshness, indicator, signal, 3× Z-Gap facts, decision, …) with **synchronous flush** → filesystem I/O on the evaluation path.
- N7 compose: seal and summary written as JSON documents; evaluation stubs appended in-memory then dumped at end.
- Reporting write failures typically surface as host/tool exceptions (no separate reporting-health channel or drop counters today).
- Crash-safe property: JSONL is append-only (partial runs retain prior lines); N7 report is written at terminalization (partial compose may still exist under `compose/`).

---

## 2. `var/` inventory (present tree)

**Quantified at audit time (PowerShell recursive listing):**

| Metric | Value |
|--------|------:|
| Total files under `var/` | **44** |
| Approximate total size | **~0.70 MiB** (733 267 bytes) |
| `var/reporting/` | 29 files / ~0.66 MiB |
| `var/state/` | 10 files / ~0.03 MiB |
| `var/tmp_r7e_debug/` | 4 files / ~0.01 MiB |

**By extension:** `.json` ×36 (~0.23 MiB), `.jsonl` ×8 (~0.46 MiB).

**Largest artifacts (top):**

| Size | Path |
|-----:|------|
| ~0.10 MiB | `var/reporting/yaml_run/pytest_yaml_shadow/shadow_facts.jsonl` |
| ~0.10 MiB | `var/reporting/yaml_run/cli_yaml_shadow/shadow_facts.jsonl` |
| ~0.09 MiB | `var/reporting/yaml_run/pytest_yaml_observe/observe_facts.jsonl` |
| ~0.09 MiB | `var/reporting/yaml_run/cli_yaml_observe/observe_facts.jsonl` |
| ~0.04 MiB | `var/reporting/yaml_run/yaml_live_20260724T224238Z/compose/summary.json` |
| ~0.04 MiB | `var/reporting/n7/oneshot_20260723T074255Z/compose/summary.json` |
| ~0.03 MiB | `var/reporting/n5/z_gap_shadow_facts.jsonl` |

**Important inventory caveat:** the current `var/` tree is a **small residual** of recent YAML/N7/N5 work. Many producers (N1 capture, N2 smoke, N3 PTB, R6/R7 defaults, large raw market captures) are **capable of writing** under `var/reporting/` but have **no present artifacts** in this workspace snapshot. Historical large JSONL volumes are therefore under-represented; code audit fills that gap (§3–4).

### 2.1 Present directory families

| Path | Apparent origin |
|------|-----------------|
| `var/reporting/yaml_run/z_gap_aggressive_{test,shadow}/` | Operator/manual YAML OBSERVE/SHADOW |
| `var/reporting/yaml_run/cli_yaml_{observe,shadow}/` | CLI tests (`test_yaml_run_cli.py`) writing into real `var/` |
| `var/reporting/yaml_run/pytest_yaml_{observe,shadow}/` | Test/CLI naming residual in real `var/` |
| `var/reporting/yaml_run/yaml_live_20260724T224238Z/` | Armed LIVE run (case study) |
| `var/reporting/yaml_run/yaml_live_{fake,readonly}_dev/` | Dev rehearsals during implementation |
| `var/reporting/n7/oneshot_20260723T074255Z/` | Prior N7 oneshot |
| `var/reporting/n5/z_gap_shadow_facts.jsonl` | N5 shadow facts residual |
| `var/reporting/n4/_pytest_fixture_summary.json` | Pytest residual |
| `var/state/**` | Shadow snapshots + R7 ack/residuals |
| `var/tmp_r7e_debug/**` | Ad-hoc R7e debug nest |

---

## 3. Artifact catalog

Columns: Path/pattern · Mode(s) · Producer · Trigger · Format · Frequency · Typical volume · Consumer · Purpose · Analytical · Operational · Duplication/noise · Recommendation

| Path/pattern | Mode(s) | Producer | Trigger | Format/schema | Freq | Typical volume | Current consumer | Purpose | Analytical value | Operational value | Duplication/noise | Recommendation |
|---|---|---|---|---|---:|---:|---|---|---|---|---|---|
| `var/reporting/yaml_run/<run>/*_facts.jsonl` | OBSERVE/SHADOW | `ObserveHost`/`ShadowHost` + `JsonlFactSink` via YAML `run` | each eval + lifecycle | FactEnvelope v1 JSONL | high per eval | 40–220 lines/fixture run; grows with live duration | humans, ad-hoc scripts; no first-class analyzer | reconstruct decisions + shadow lifecycle | **High** (`zgap_calibration_row`) | Medium | Per-eval fan-out (~8–11 facts/eval) is verbose but informative | Keep as analytical channel; add aggregation/summary; sampling optional |
| `zgap_calibration_row` (inside facts) | OBSERVE/SHADOW | `build_calibration_row` via `ZGapBinding` | every Z-Gap eval | payload schema `z_gap_calibration_v1` | 1/eval | small object | none automated | strategy research | **Highest present** | Low | Overlaps model/valuation facts | Promote to first-class decision record |
| `zgap_model_snapshot` / `zgap_entry_valuation` | OBSERVE/SHADOW | `ZGapBinding.extra_facts` | every eval | JSON payload | 1/eval each | small | none automated | model/edge detail | High | Low | Partial overlap with calibration row | Keep under debug/analysis profile; avoid triple-writing identical fields in default |
| `observe_decision` / `signal` / `freshness_assessment` / `timer_elapsed` / `indicator_result` | OBSERVE/SHADOW | host `_emit` | eval pipeline | FactEnvelope | high | dominates line count | none automated | host pipeline proof | Medium/Low | Medium (freshness) | Repeated scaffolding | Aggregate or sample unchanged WAIT/SKIP |
| Shadow OMS/lifecycle facts | SHADOW | `ShadowHost` | order/fill/exit | FactEnvelope | per transition | moderate | tests | paper execution proof | Medium | High for SHADOW validation | Distinct from strategy facts | Retain lossless for transitions |
| `var/reporting/yaml_run/<live>/n7_oneshot_report.json` | LIVE | `n7_operator_run` + `yaml_config.live_run` merge | session end | JSON summary | 1/run | ~15–30 KB | operator console | safety + outcome | Low for gates | **Highest for LIVE ops** | Duplicates sealed/preflight subsets | Keep mandatory; extend with analytics summary (future) |
| `.../effective_n7_sealed.json` | LIVE | `yaml_config.n7_bridge.write_effective_n7_sealed` | live start | JSON | 1/run | small | preflight loader | resolved sealed config | Medium (params) | High | Also embedded in report | Keep; treat as manifest fragment |
| `.../preflight/*.json` | LIVE | `n7_preflight` / `live_preflight` | before mutations | JSON | 2 + summary | small | operator | auth/connectivity/recon | Low | **Mandatory safety** | Restart pair is intentional | Keep; redact secrets |
| `.../compose/summary.json` | LIVE / N4 compose | `live_zgap_compose` | compose end | JSON | 1/run | tens of KB with 159 stub obs | humans | feeds, seals, obs stubs | **Low in N7** (stubs lack gates) | Medium | 159 near-duplicate stubs | Replace stubs with decision records + aggregates |
| `.../compose/seal_*.json` | LIVE / N4 | `live_zgap_compose` | each seal | JSON | 1/window | tiny | humans | PTB seal evidence | Medium (K) | High | Also in report.seal | Keep |
| `var/reporting/n7/oneshot_*/**` | LIVE (legacy CLI) | `tools/n7_live` / `n7_operator_run` | N7 tool | same as yaml live | 1/run | small–medium | operators | same N7 lifecycle | same LIVE gap | High | Parallel tree to `yaml_run` | Compatibility retain; converge naming later |
| `var/reporting/n5/*.jsonl` | SHADOW (N5) | N5 shadow runtime/tools | N5 run | JSONL | per eval | small residual here | N5 tools | N5 lifecycle proof | Medium | Medium | Separate from YAML SHADOW | Keep family; normalize later |
| `var/reporting/n4/*` | OBSERVE (N4) | N4 tools/tests | N4 run | JSON | rare residual | tiny here | N4 tools | observe composition | Medium when full summary present | Medium | Parallel to YAML OBSERVE | Keep producer; unify schemas later |
| `var/reporting/n1/**` (code default) | audit | `tools/n1_audit/*` | capture/analyze | JSONL/JSON | high when used | **can be huge** (raw ticks) | n1 analyzers | feed/PTB research | High for feed science | Low for trading | Raw capture is intentionally heavy | Separate raw channel; not default trading report |
| `var/reporting/n2/**` | smoke | `tools/n2_smoke/*` | smoke/diagnose | JSON | per smoke | small | humans | connectivity | Low | High ops | — | Keep tool-local |
| `var/reporting/n3*/**` | PTB | `tools/n3_ptb/*` | seal campaign | JSON | per campaign | medium | humans | PTB seal proof | Medium | High | Overlaps compose seals | Keep |
| `var/reporting/r6|r7|r7b|r7c|r7d|n6/**` | legacy LIVE/preflight | CLI + `r7b_live_once` etc. | operator/tools | JSON/JSONL | per run | variable | operators/scripts | R6/R7 evidence | Mixed | High historical | Parallel taxonomies | Compatibility; do not delete; avoid new default dependence |
| `var/state/z_gap*_snapshot.json` | SHADOW | Shadow persistence | promote/save | JSON | intermittent | small | ShadowHost restart | local OMS snapshot | Low | Medium | Config default path | Keep durable; not analytics |
| `var/state/r7/**` | R7 live | ack/residual writers | regenerate/recon | JSON | durable | small | R7 ops | identity/residuals | Low | **Mandatory ops** | Must not live only under reporting | Keep outside disposable reporting |
| `var/tmp_r7e_debug/**` | debug | ad-hoc | manual | mixed | rare | tiny | developer | debug nest | Low | Low | Nested `var` inside tmp | Treat as debug residue; optional cleanup later (not this task) |

### 3.1 Classification of each family

1. **Mandatory safety/audit:** N7/yaml live report core fields, preflight, seal, mutation counts, residuals/ack under `var/state`, order/fill facts when mutations occur.  
2. **Operational health:** preflight, feed counts, clock sync, connectivity diagnoses (N2).  
3. **Debug/developer trace:** timer/freshness/indicator fan-out; R7e tmp; full raw N1 captures.  
4. **Strategy decision analytics:** `zgap_calibration_row` (+ model/valuation) — **present in OBSERVE/SHADOW hosts; absent from N7 live stubs**.  
5. **Risk analytics:** sparse as dedicated records (mostly embedded in deny reasons when risk runs).  
6. **Execution/lifecycle analytics:** SHADOW OMS facts; N5 lifecycle; R7b facts; N7 entry/exit/econ when a trade occurs.  
7. **Post-run summary:** `n7_oneshot_report.json`, tool `*_summary.json`.  
8. **Raw market-data/reproduction:** N1 raw capture (producer exists; not present in current tree).  
9. **Test-generated evidence:** `cli_yaml_*`, `pytest_yaml_*`, `n4/_pytest_*` under real `var/`.  
10. **Legacy/duplicated/unconsumed:** R6/R7 trees (defaults in CLI), parallel N7 vs `yaml_run` live layouts, triple Z-Gap fact types overlapping calibration.

---

## 4. Reporting architecture trace (source references)

### 4.1 End-to-end path

1. **Market data / indicators** — adapters + host freshness/indicator emission (`ObserveHost` emits `freshness_assessment`, `indicator_result`).  
2. **Strategy evaluation** — `ZGapBinding.evaluate` (`src/tyrex_pm/runtime/strategy_binding.py` ~415–508) builds:
   - `StrategyDecision` (`action`, `reason_code`, `evidence`)
   - intents
   - `extra_facts`: `zgap_model_snapshot`, `zgap_entry_valuation`, `zgap_calibration_row` (`build_calibration_row` in `strategies/z_gap/calibration.py`)
3. **Intent → risk → plan → OMS** — ObserveHost may stop at intents; ShadowHost continues into ShadowOMS; N7 uses `N7OneShotHost` after first EnterIntent.  
4. **Portfolio / lifecycle / recon** — shadow/N5/N7 hosts; economics in N7 `economics_report()`.  
5. **Writers → `var/`** — `JsonlFactSink.append` or JSON `write_text` of summaries/reports.

### 4.2 Critical loss point (LIVE)

`src/tyrex_pm/runtime/n7_live_session.py` `_capture_intents` (approx. lines 60–113):

- Calls `ready.binding.evaluate(...)` — **full `StrategyEvalResult` including `extra_facts` exists in memory**.
- Persists only:
  - `evals` counter
  - `last_decision = type(result.decision).__name__` → always `"StrategyDecision"` (class name, **not** `action`/`reason_code`)
  - `intent_types`, `enter_captured`, `sealed_k`, `model_anchor_k`
- **Discards:** `decision.action`, `decision.reason_code`, `decision.evidence`, entire `extra_facts` (z, σ, τ, edges, books, calibration).

Those stubs are what `live_zgap_compose` stores in `compose/summary.json` → `observations[]`.

### 4.3 Contrast: N4 runtime retains more (but still incomplete)

`N4ObserveRuntime` evaluation path builds `ObserveRecord` with `tau_s`, `p_up`/`p_down`, basis, binance/chainlink raw, `c_hat`, etc. (`n4_observe_runtime.py` ~750–811). Even there, `edge_summary=None` and primary reject reason is not always the strategy `reason_code`. N7 does **not** use this richer record for its callback observations.

### 4.4 Gate ordering in strategy (for interpreting future aggregates)

`evaluate_readiness` then leg selection (`policies.py`):

1. time ready → PTB usable → PTB lag → jump guard → model ready → basis → τ band → |z| block → |z| band  
2. then `select_leg`: book/fee readiness inside valuations → edge vs `theta_take` → both-legs / tie rules  

**Primary reason** is sequential: later gates are not evaluated once readiness fails. Aggregates must distinguish **primary reject** vs **all failed checks among evaluated gates**.

### 4.5 Values calculated but lost on LIVE path

| In-memory source | Fields | Lost at |
|---|---|---|
| `ZGapModelSnapshot` / calibration row | `z`, `sigma`, `tau_s`, `K`, `S`, `p_up`/`p_down`, basis, books | `_capture_intents` return |
| `EntryLegValuation` | `e_settlement`, `e_repricing`, asks | same |
| `StrategyDecision` | `action`, `reason_code`, `evidence` (selected leg, edges) | replaced by class name string |
| `LegSelectionResult.evidence` | `e_up`/`e_down` | never copied into N7 report |

---

## 5. Case studies

### 5.1 OBSERVE — `var/reporting/yaml_run/z_gap_aggressive_test/observe_facts.jsonl`

| Question | Answer from evidence |
|---|---|
| Immediate | Fixture OBSERVE; 40 JSONL lines; 4 calibration rows; actions `WAIT`; reason `MODEL_NOT_READY` |
| Correlation needed | Map `epoch_id` across fact types; thresholds from resolved YAML (not always embedded per row) |
| Duplication | model snapshot + entry valuation + calibration + signal + observe_decision |
| Verbose? | Manageable for fixture; would grow on live public OBSERVE |
| Unanswerable? | Little — reason codes present |
| Reconstructible? | Yes for decisions; not full raw books beyond ask/bid fields in calibration |
| Strategy explainable? | **Yes** (`reason_code`, z/τ/K/S when ready) |
| Execution? | N/A (no OMS) |
| Comparable? | Yes to other OBSERVE JSONL with same fact types |

### 5.2 SHADOW — `var/reporting/yaml_run/z_gap_aggressive_shadow/shadow_facts.jsonl`

Same analytical facts as OBSERVE plus shadow lifecycle facts when OMS engages. This fixture stayed `MODEL_NOT_READY` / no trades — strategy explainable; execution path unused.

Larger CLI/test shadows (`cli_yaml_shadow`, 220 lines) show the per-eval fan-out pattern clearly.

### 5.3 Armed LIVE no-entry — `var/reporting/yaml_run/yaml_live_20260724T224238Z/`

Located by contents: `outcome=PASS_N7_SAFE_NO_ENTRY`, `reason=evaluated_no_enter_signal`, `evals=159`, `live_armed=true`, `real_venue_mutations=0`.

| Question | Answer |
|---|---|
| Immediate | Preflight GO; sealed K=`64122.086130005`; PTB authority chainlink; SSR disabled; 159 evals; zero EnterIntent; zero mutations |
| Correlation | Report ↔ compose/summary ↔ seal ↔ preflight — mostly duplicated seals/config |
| Duplication | seal fields in report + seal file + summary.seals; full resolved YAML embedded in report |
| Verbose? | 159 nearly identical observation stubs (`decision: StrategyDecision`, empty intents) — high noise, low info |
| Strategy explainable? | **No** — cannot answer gate/edge/z/τ distributions |
| Execution? | N/A (no orders); mutation boundary proven |
| Reconstructible? | Ops/safety yes; decision math **no** |
| Comparable? | Ops outcomes yes; research metrics **no** |

#### 5.3.1 Can existing LIVE evidence answer research questions?

| Question | Answerable now? |
|---|---|
| τ distribution | **No** |
| Binance/spot vs sealed K | **No** (only seal K; feed counts only) |
| σ and z | **No** |
| Model probability | **No** |
| YES/NO bid/ask/spread/depth | **No** |
| Gross / net edge, fees, slippage | **No** |
| Thresholds | **Yes** (embedded `resolved_strategy` / risk) — but not per-eval comparisons |
| Which entry gate failed / frequencies | **No** |
| Closest-to-entry evaluation | **No** |

### 5.4 Authenticated LIVE dry / prior N7

- `yaml_live_readonly_dev`: preflight path (environment may NO_GO); still zero mutations; no analytics channel.  
- `n7/oneshot_20260723T074255Z`: same N7 shape as yaml live (ops-focused).

---

## 6. Highest-volume noise vs highest-value artifacts

### 6.1 Highest-volume / noisiest (present tree)

1. OBSERVE/SHADOW JSONL **per-eval multi-fact fan-out** (timer/freshness/indicator/signal + 3 Z-Gap facts + decision).  
2. LIVE `compose/summary.json` **observation stubs** × N (159 near-duplicates).  
3. (Code-capable, not present) N1 **raw capture JSONL** — designed to be largest when used.

### 6.2 Highest-value existing artifacts

1. `zgap_calibration_row` in OBSERVE/SHADOW facts — closest to research needs.  
2. N7/yaml `n7_oneshot_report.json` — safety, PTB, mutations, terminal outcome.  
3. Preflight GO/NO_GO packages — auth/connectivity/recon.  
4. Seal records — immutable K authority evidence.  
5. Shadow OMS/lifecycle facts — paper execution truth.

### 6.3 Duplicated / ambiguous evidence

- Seal/PTB fields repeated across report, seal file, summary.  
- Resolved config embedded in live report **and** `effective_n7_sealed.json`.  
- Triple Z-Gap fact types overlapping.  
- Ambiguity: LIVE observation `decision: "StrategyDecision"` looks like a decision label but is only a Python class name.

---

## 7. Missing diagnostics (summary)

- Cross-mode **run manifest** with schema version + code SHA + reporting profile.  
- Persisted **primary gate / reason** on LIVE evaluations.  
- Gate failure histograms + closest candidate.  
- Intended vs executable vs realized economics linkage.  
- Reporting-health (drops, flush errors, partial run flag).  
- Stable cross-strategy decision envelope (OBSERVE JSONL ≈ good start; LIVE/N7 not wired).

---

## 8. Exact reason the armed no-entry run cannot be explained

The strategy **did** compute full diagnostics on each of the 159 evaluations inside `ZGapBinding.evaluate` (including `StrategyDecision.reason_code` and `zgap_calibration_row` fields).

The N7 live callback `_capture_intents` **threw those values away**, persisting only a thin stub. Downstream `compose/summary.json` and `n7_oneshot_report.json` therefore prove “evaluated without EnterIntent” but cannot reconstruct **why**.

This is a **reporting selection bug/gap in the LIVE host path**, not absence of strategy calculation.

---

## 9. Performance risks in the current reporting path

| Risk | Evidence |
|---|---|
| Sync flush every fact | `JsonlFactSink.flush_every=True` default |
| Multi-fact amplification | ~8–11 JSONL lines per OBSERVE eval in samples |
| LIVE stub list growth | 159 objects in one summary JSON (low CPU, poor S/N) |
| Large raw captures (when enabled) | N1 tools write unbounded tick JSONL under `var/reporting/n1/` |
| No drop counters / backpressure | Missing reporting-health channel |
| Hot-path coupling | Host `_emit` inline with evaluation |

No microbenchmark harness for reporting overhead was found; acceptance criteria must be relative to existing host eval rates (see recommendation doc).

---

## 10. Tests / tools that consume or pollute `var/`

### 10.1 Tests writing into real `var/` (finding only)

- `tests/test_yaml_run_cli.py` uses `--run-name cli_yaml_observe|cli_yaml_shadow` → writes under `var/reporting/yaml_run/` (present artifacts).  
- Residuals named `pytest_yaml_*` likewise under real `var/`.  
- `var/reporting/n4/_pytest_fixture_summary.json` indicates N4 tests/tools touching real `var/`.  
- Many other tests correctly use `tmp_path` (F3/F4/F5, etc.).

### 10.2 Tools / docs consumers

- `tools/n1_audit/*`, `n2_smoke/*`, `n3_ptb/*`, `n4_observe/*`, `n5_shadow/*`, `n6_live/*`, `n7_live/*` read/write `var/reporting/...` as defaults.  
- Docs: `Docs/latest/modules/reporting_and_operations.md`, how-to run modes.  
- No first-class notebook analyzer for `zgap_calibration_row` was found in-repo.

---

## 11. Operational vs analytical vs debug (as currently mixed)

| Category | Today’s primary homes | Mixed? |
|---|---|---|
| A. Operational/safety | N7 report, preflight, seal, mutation counts, `var/state` | LIVE report also embeds full strategy YAML (good) but not eval analytics |
| B. Strategy/research | OBSERVE/SHADOW JSONL calibration | **Not connected to LIVE** |
| C. Debug/high-volume | timer/freshness/indicator facts; N1 raw; compose stub spam | Mixed into default OBSERVE JSONL |

---

## 12. Conclusions (required)

1. **How it works today:** Host-specific writers; OBSERVE/SHADOW use `JsonlFactSink` + rich Z-Gap `extra_facts`; LIVE/N7 uses operator JSON + compose seals/stubs and **drops** strategy diagnostics after `evaluate()`.  
2. **Complete classification:** See §3 catalog (safety, ops, debug, analytics, execution, summary, raw, test, legacy).  
3. **Highest-volume noise:** per-eval fact fan-out (OBSERVE/SHADOW); LIVE observation stubs; (latent) N1 raw captures.  
4. **Highest-value artifacts:** `zgap_calibration_row`; N7 oneshot report/preflight/seal; shadow lifecycle facts.  
5. **Duplicated/ambiguous:** seals/config repeated; LIVE `decision` class-name ambiguity; overlapping Z-Gap fact types.  
6. **Missing diagnostics:** LIVE per-eval action/reason/gates/edges/books; run-level gate histograms; reporting-health; cross-mode unified decision schema.  
7. **Why no-entry LIVE cannot be explained:** `_capture_intents` discards `StrategyEvalResult.extra_facts` and `decision.reason_code` after computing them.  
8. **Performance risks:** synchronous per-fact flush; multi-fact amplification; unbounded raw tool captures; no backpressure metrics.  
9. **Consumers / polluters:** N1–N7 tools; YAML CLI tests writing real `var/reporting/yaml_run/*`; docs describing report fields.  
10. **Unresolved questions:** (a) historical peak `var/` sizes on operator machines with N1 captures; (b) whether N4 live summaries are still used operationally alongside YAML OBSERVE; (c) desired retention of R7b fact taxonomy vs convergence; (d) whether LIVE must remain JSON-only or may add JSONL beside N7 report without breaking acceptance scripts.

---

## Appendix A — JSONL type frequencies (present samples)

| File | Lines | Dominant types |
|---|---:|---|
| `cli_yaml_observe/observe_facts.jsonl` | 190 | timer/freshness/indicator/signal/zgap_*/observe_decision ×20 |
| `cli_yaml_shadow/shadow_facts.jsonl` | 220 | same + shadow extras |
| `z_gap_aggressive_test/observe_facts.jsonl` | 40 | ×4 evals |
| `n5/z_gap_shadow_facts.jsonl` | 30 | zgap_* + `n5_shadow_evaluation` + `persistence_saved` ×6 |

## Appendix B — Producer inventory (code-capable, even if no artifact now)

| Producer module | Writes |
|---|---|
| `reporting/jsonl.py` | JSONL sink primitive |
| `runtime/observe_host.py`, `shadow_host.py` | facts JSONL |
| `runtime/yaml_config/adapt.py`, `live_run.py`, `n7_bridge.py` | yaml_run paths |
| `runtime/n7_operator_run.py`, `n7_live_session.py`, `n7_preflight.py` | N7 report/preflight |
| `runtime/live_zgap_compose.py` | compose summary/seals |
| `runtime/r7b_live_once.py` | r7b facts+report |
| `application/cli.py` | default out paths for preflight/r7/n6/n7 |
| `tools/n1_audit/*` … `tools/n7_live/*` | stage evidence trees |
| Shadow/R7 state writers | `var/state/**` |
