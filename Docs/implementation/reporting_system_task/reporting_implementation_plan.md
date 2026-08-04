# Reporting system — implementation plan (final revision)

**Status:** M1–M8 implemented (owner-authorized one-shot cutover)  
**Depends on:** `current_reporting_audit.md`, `reporting_design_recommendation.md`, owner decisions (2026-07-27)  
**Plan date:** 2026-07-27 (final pre-implementation revision); implementation completed 2026-07-27  
**Repository HEAD verified:** `rest_project` @ `18235c84c99bb0066bcc5271bd761fb191b0548c`

> Supersedes earlier “planning only — not authorized to implement” wording. Owner message authorized full M1–M8 implementation.  
> Completion evidence: `reporting_implementation_report.md`.

---

## 0. Branch and HEAD

| Field | Value |
|-------|-------|
| Branch | `rest_project` |
| HEAD | `18235c84c99bb0066bcc5271bd761fb191b0548c` |
| HEAD subject | `Add YAML run --mode live that reuses the N7 one-shot host.` |

Historical artifacts under `var/` are evidence only and must not be rewritten.

---

## 1. Concise current-state findings (verified)

1. **Fragmented reporting.** OBSERVE/SHADOW: `ObserveHost._emit` → `JsonlFactSink` (`runtime/observe_host.py`, `reporting/jsonl.py`). LIVE: N7 JSON writers (`n7_operator_run._write_report`, `live_zgap_compose`, `n7_preflight`) under `var/reporting/yaml_run/` or `var/reporting/n7/`.
2. **OBSERVE/SHADOW are analytically rich.** `ZGapBinding.evaluate` builds `StrategyDecision` + `extra_facts` (`zgap_model_snapshot`, `zgap_entry_valuation`, `zgap_calibration_row`) in `runtime/strategy_binding.py` (~415–508).
3. **LIVE loses strategy diagnostics.** `n7_live_session._capture_intents` (L60–113) discards `action`, `reason_code`, `evidence`, and `extra_facts` after `evaluate()`.
4. **Taxonomy mixed.** `var/reporting/**` (disposable), `var/state/**` (durable via `r7_paths.py`), raw N1 captures under reporting paths.
5. **Tests pollute real `var/`.** `tests/test_yaml_run_cli.py` writes `cli_yaml_*` under `var/reporting/yaml_run/`.
6. **Hot path sync FS I/O.** `JsonlFactSink(flush_every=True)`; ~8–11 facts per OBSERVE evaluation.
7. **Problem is organization/consistency/usability**, not current storage volume.
8. **No unified summary.** LIVE has ops-first `n7_oneshot_report.json`; OBSERVE/SHADOW have JSONL only.

---

## 2. Approved target architecture (frozen)

```text
OBSERVE = strategy explanation
SHADOW  = strategy explanation + simulated execution
LIVE    = strategy explanation + real execution
```

```text
runtime components (already own values)
  → ReportingEvent (common envelope + optional strategy_diagnostics)
  → ReportingPort / RunReporter.emit(...)
       ├─ update SummaryAccumulators (summary.py)  — always, before sampling
       └─ route to exactly two lanes:
            Critical/audit  → audit_events.jsonl
            Optional analytics → analytics_events.jsonl [/ diagnostics/]
  → background batched writer + periodic atomic summary checkpoints
  → finalize → terminal run_summary.json + manifest.json
```

| Concern | Owner | Must not own |
|---------|-------|--------------|
| Envelope, lanes, writer, summary, paths, config | `tyrex_pm.reporting` | strategy mathematics |
| Strategy diagnostics | `strategies/z_gap/reporting.py` | generic core strategy switches |
| Spec registration | composition / binding layer supplies registered diagnostics contract | hard-coded strategy import list inside reporting core |
| Host wiring | `observe_host`, `shadow_host`, `n7_*`, `yaml_config.*` | direct strategy file writes |
| Runtime state | persistence + R7 modules under `var/runtime_state/` | report cleanup |
| Raw recordings | N1/N3 raw capture → `var/recordings/` | default trading report |

**Hard rules (do not reopen):** one reporter for all modes; compact package; strategy-owned diagnostics; no math recompute; complete summary counters before sampling; two lanes; `full.yaml` default; mandatory evidence never disableable; LIVE fail-closed for new exposure on critical audit failure; no dual write / feature flag / compatibility fallback; same-milestone deletion of replaced producers; one-time `var/state` → `var/runtime_state` migration; no database; clean cutover.

---

## 3. Compact package structure

```text
src/tyrex_pm/reporting/
├── __init__.py      # open_run_reporter, RunReporter, ReportingPort
├── contracts.py     # envelope, lineage, health enums, registration Protocol
├── reporter.py      # emit, lane routing, seq, health transitions, finalize, checkpoint trigger
├── writer.py        # bounded queues, batched JSONL, atomic JSON replace, durable ack
├── summary.py       # SummaryAccumulators + build/checkpoint run_summary.json
├── config.py        # load/validate reporting YAML (optional analytics only)
├── paths.py         # var/runs, var/runtime_state, var/recordings, var/logs, _ops
└── adapters.py      # eval/risk/OMS/lifecycle/recon/N7-ops → events

src/tyrex_pm/strategies/z_gap/
└── reporting.py     # diagnostics builder + diagnostics_schema_version + gate records
```

`SummaryAccumulators` lives only in `summary.py` (not duplicated in `contracts.py`).  
Public surface name is **`ReportingPort`** (not RecordingPort — avoids confusion with raw recordings).

Existing `reporting/jsonl.py`, `facts.py`, `serialize.py` are absorbed into `writer.py` / `contracts.py` and deleted when hosts no longer reference them (M4 for host path; M8 for any residual unused exports).

Do not add new modules solely for checkpointing, health, or schemas — fold into the files above.

---

## 4. Common envelope and version ownership

### 4.1 Required shared envelope fields

| Field | Notes |
|-------|-------|
| `schema_version` | common reporting contract (string/semver family) |
| `event_id` | unique event id |
| `run_id` | run identity |
| `strategy_id` | when applicable |
| `mode` | `observe` \| `shadow` \| `live` \| `_ops` |
| `sequence` | monotonic per-run `seq` |
| `event_family` / `event_type` | family + concrete type |
| `event_time` | domain/event time (UTC) |
| `record_time` | persistence/record time (UTC) |
| `producer` | component id (e.g. `observe_host`, `risk_engine`, `z_gap.reporting`) |
| `lineage` | evaluation_id → … → exit_id (nulls until known) |
| `payload` | family-specific body |
| `strategy_diagnostics` | optional `{namespace, schema_version, values}` |

Lineage:

```text
evaluation_id → decision_id → intent_id → risk_decision_id
  → execution_plan_id → order_id → fill_id → position_id → exit_id
```

### 4.2 Version ownership (do not merge; no per-payload common versions)

| Version | Meaning | Owner |
|---------|---------|-------|
| `schema_version` | common envelope + common payloads | `reporting.contracts` |
| `strategy_version` | strategy logic that decided | strategy/binding constant |
| `diagnostics_schema_version` | one strategy’s diagnostics blob | `strategies/<id>/reporting.py` |
| `config_hash` | resolved parameters identity | manifest |
| `git_head` / dirty | repository source | manifest |
| package version | distribution identity | manifest only |

Common payloads evolve with `schema_version` only. Do not invent independent versions for every small common payload in v1.

### 4.3 Strategy diagnostics registration

Composition/binding supplies a registered diagnostics contract when constructing the reporter (Protocol in `contracts.py`). Reporting core must **not** maintain a hard-coded import list of strategies. Z-Gap validates its own typed payload in `strategies/z_gap/reporting.py`. Core validates envelope + accepts opaque namespaced diagnostics (redaction only). Future strategies integrate by composition/registration without editing reporting core logic.

---

## 5. Implementation-ready event-family schema

| Event family | Producer / symbols (current) | Required payload (conceptual) | Lane | Summary effect |
|--------------|------------------------------|-------------------------------|------|----------------|
| Data health / input | `ObserveHost.build_snapshot` + `assess_freshness` (`observe_host.py`); N4 prepare_aligned_eval skip reasons; LIVE feed readiness | source, source_ts, receive_ts, freshness, validity, failure reason | Analytics unless safety-critical → Critical | `data_health`, skipped-eval counts |
| Indicator / signal | `ObserveHost.evaluate_once` emits `indicator_result`, `signal`; binding `signal_payload` | name/type, value, validity, event time, input lineage | Analytics | indicator/signal counts |
| Decision / gates | `ZGapBinding.evaluate` → `StrategyDecision`; `strategies/z_gap/reporting.py` (new) from `policies.evaluate_readiness` / `select_leg` / calibration | action, primary reason, all gate outcomes (passed/failed/not_evaluated), diagnostics namespace/version | Analytics; **Critical if intent emitted or BLOCKED safety** | actions, rejection & gate distributions, closest candidates |
| Intent / risk / plan | `EnterIntent`/`ExitIntent`/… (`core/intents.py`); `RiskEngine`/`RiskDecision` (`risk/decision.py`); `PlanningResult` (`planning/plan.py`); hosts `_emit` `intent_*`, `risk_*`, `execution_plan_*` | lineage IDs, requested action, decision, result, reason, limits | Critical | intent/risk/plan counts |
| Order / fill / cancel | `ShadowOMS` / `LiveOMS` (`execution/shadow_oms.py`, `execution/polymarket/live_oms.py`); ShadowHost `_execute_approved` | client/venue IDs, request, response, status, qty, price, fees | Critical | execution section |
| Position / lifecycle | `lifecycle/trade_lifecycle.py`; `ShadowHost._on_lifecycle_transition` | prior/new state, qty, exposure, reason | Critical | position/lifecycle counts |
| Exit / flatten / recon | lifecycle exits; `ReconcileReport` (`execution/polymarket/reconciliation.py`); R7 residuals; N7 `economics_report` | reason, intended vs actual exposure, residuals, discrepancies | Critical | final outcome / residuals |
| Reporting health / fatal | `RunReporter` / `writer.py` | health transition, error class, counters, affected lane | Critical | reporting completeness; distinct from run terminal status |
| Operational / LIVE safety | `n7_preflight`, seal in `live_zgap_compose`, arming in `n7_operator_run` / `live_run` | preflight result, seal/PTB trust, live arm, mutation boundary | Critical | status + LIVE identity |

Adapters in `adapters.py` map these existing values — they do not recompute them.

---

## 6. Z-Gap values to emit (never recompute)

Sources today: `ZGapDecisionSnapshot`, `ZGapModelSnapshot`, `EntryLegValuation`, `StrategyDecision`, `build_calibration_row`, `evaluate_readiness` / `select_leg` / `combine_precedence`, `ZGapBinding.evaluate` extras, intent fields.

| Value | Current owner / symbol | Common vs diagnostics | Summary? | Detail level |
|-------|------------------------|----------------------|----------|--------------|
| evaluation / epoch / window / market ids | `ZGapDecisionSnapshot.epoch`, `market_id`, `window_id` | common lineage + payload ids | yes (identity) | mandatory on decision |
| action, primary `reason_code`, `decision_id` | `StrategyDecision` | common decision | yes | mandatory |
| all gate outcomes + not-evaluated | derive from readiness/selection path in `strategies/z_gap/reporting.py` using existing `ReadinessResult` / `LegSelectionResult` / config thresholds — **record comparisons already decided**, do not re-run math | common `gates[]` | yes (distributions) | mandatory structure; dense values per profile |
| selected leg | `decision.evidence["selected_leg"]` / `LegSelectionResult` | common + diagnostics | closest / entry | when present |
| z, sigma, tau_s | `ZGapModelSnapshot` | diagnostics | closest optional | full analytics |
| K (PTB), S (settlement/ref), basis_bps | model / `PtbSnapshot` / `compute_basis_bps` via assemble | diagnostics | data_health if reject | full |
| PTB quality / usable / lag / attestation | `PtbSnapshot`, N7 seal fields | diagnostics + LIVE audit seal event | LIVE status | mandatory on LIVE seal |
| Binance/Chainlink refs + timestamps | assemble / N4 dyn (`binance_raw`, `chainlink_raw`, source_ts) | diagnostics refs / timestamps | data_health | full; raw streams → recordings |
| p_up / p_down | `ZGapModelSnapshot` | diagnostics | no (detail) | full |
| up/down bid/ask | `LegBookView` / calibration row | diagnostics | no | full |
| book/fee readiness | valuation `ready`, `fee_curve`, `fee_resolved` | gates + diagnostics | gate counts | full |
| fee_rate / exponent / fee label | `fee_curve` / calibration `"estimated"` | diagnostics | performance assumptions SHADOW/LIVE labels | full |
| slippage assumptions | `ZGapRealizationConfig.slippage_included_in_executable_bid`; ShadowOMS `assumptions_fact()` | diagnostics + SHADOW performance | yes (assumptions) | mandatory in SHADOW summary |
| executable net edge (`e_repricing` or `e_settlement` per config) | `EntryLegValuation` | diagnostics + closest | closest | when edge evaluated |
| required edge (`theta_take` / fill floor) | `ZGapEntryConfig` | diagnostics + closest | closest | when edge evaluated |
| signed edge margin | edge − threshold (from already computed edge + config threshold; builder records margin, does not re-derive fair values) | diagnostics + closest | closest | when edge evaluated |
| target notional / qty | `ZGapDecisionSnapshot.target_notional`; `EnterIntent` | common intent | intent counts | on intent |
| risk / plan / execution lineage | host + `RiskDecision.decision_id`, plan ids, OMS ids | common lineage | yes | critical path |
| exit / hold / flatten reason | `StrategyDecision.reason_code` / lifecycle | common | yes | critical on transition |
| position context | `PositionView` / `zgap_active_position_context` | diagnostics | position section | when held |

A future strategy provides a different typed diagnostics payload via composition/registration; reporting core stays strategy-agnostic.

---

## 7. Two lanes and reporting-health states (frozen)

### 7.1 Lanes

**Critical/audit:** authorization, safety failures, intents/risk/plans, orders/fills/cancels, positions, exits/flatten, recon/residuals, fatal errors, reporting-health transitions, LIVE seal/arm/mutation-boundary, **pre-mutation audit records**. Never silently dropped.

**Optional analytics:** per-evaluation detail, indicators/signals, WAIT/SKIP rows, debug host trace. Bounded queue; may sample/drop → `DEGRADED_ANALYTICS` only.

Analytics overload must never block critical enqueue.

### 7.2 Health states

```text
HEALTHY
DEGRADED_ANALYTICS
CRITICAL_AUDIT_FAILURE
```

| State | Meaning |
|-------|---------|
| `HEALTHY` | Mandatory evidence durable; optional analytics within expected bounds |
| `DEGRADED_ANALYTICS` | Optional analytics sampled/dropped/delayed; **critical lane still healthy** |
| `CRITICAL_AUDIT_FAILURE` | Mandatory evidence cannot be durably persisted or acknowledged |

Rules:

- Analytics loss **cannot** produce `CRITICAL_AUDIT_FAILURE`.
- Critical failure **must not** be labeled only as degraded analytics.
- In LIVE, `CRITICAL_AUDIT_FAILURE` **blocks new exposure**.
- Cancel / flatten / reduce / recon remain allowed where possible.
- **Run terminal status** and **reporting health** are separate fields (both on summary + manifest/checkpoints).

---

## 8. Summary accumulators and hard-crash checkpointing

### 8.1 Accumulators (`summary.py`)

Updated on every emit **before** analytics sampling/drop. Preserve complete counts for evaluations (attempted/completed/skipped), actions, primary reasons, failed gates, gates not evaluated, data-quality failures, intents, risk, plans, orders, fills, positions, holds/exits/flatten, reporting created/written/sampled/dropped, closest candidates.

```text
total evaluations performed ≠ detailed evaluation records persisted
```

### 8.2 Periodic atomic checkpoints

In-memory accumulators alone are insufficient for hard process death. The unified reporter periodically checkpoints into the **same** `run_summary.json` path (partial) plus manifest lifecycle fields — **not** a second summary writer.

Checkpoint contents:

- partial `run_summary.json` (all sections available so far)
- lifecycle/status (`RUNNING`, partial/non-terminal)
- reporting health + counters (created/persisted/sampled/dropped by lane)
- latest durable sequence position and checkpoint timestamp

Crash-safe replace:

```text
write temporary file → flush → atomically replace target
```

Checkpoint **interval** is an internal safe default chosen during implementation validation after measuring runtime behavior — do not invent a fixed frequency in this plan.

### 8.3 Lifecycle honesty

| Situation | Behavior |
|-----------|----------|
| Normal shutdown | flush lanes → write **final** `run_summary.json` replacing last partial checkpoint → `status` terminal/complete |
| During run | latest checkpoint marked **partial / non-terminal** (`lifecycle=RUNNING`) |
| Hard crash | process cannot update status after death; **last checkpoint remains readable**, slightly stale |
| Next recovery/inspection | detect stale `RUNNING` (e.g. no heartbeat/finalize, age rule defined at implementation) → classify run **incomplete/aborted**; expose completeness flags, last checkpoint time, last durable `sequence` |
| Analytics degraded | counters complete; health=`DEGRADED_ANALYTICS` |
| Critical failure | health=`CRITICAL_AUDIT_FAILURE`; emergency critical persistence still uses the **same** writer contract |

---

## 9. LIVE pre-mutation persistence barrier (frozen)

Applies only to **exposure-increasing** venue mutations — not to every strategy evaluation.

### 9.1 Order of operations

1. Check critical/audit writer healthy (not `CRITICAL_AUDIT_FAILURE`).
2. Construct mandatory **pre-mutation audit event** from already-approved intent, risk decision, and execution plan.
3. Persist that event durably on the critical lane.
4. Receive **persistence acknowledgement** from the writer.
5. **Only then** call the venue mutation.
6. Persist venue response or failure as another critical event.
7. Update order / portfolio / lifecycle / reconciliation reporting from authoritative results.

### 9.2 Pre-mutation record must identify

- run + strategy
- intent, risk decision, execution-plan lineage
- mutation type
- market / instrument
- side, quantity, limit/budget constraints
- authorization + budget-guard result
- exposure-increasing classification (`true`)
- timestamp + deterministic sequence id

### 9.3 Post-mutation persistence failure

If the venue mutation **occurred** but its result cannot be persisted:

- set `CRITICAL_AUDIT_FAILURE` (not mere analytics degradation)
- block all subsequent exposure-increasing actions
- enter reconciliation / operator-attention handling
- continue allowing safety-reducing operations where possible
- attempt to preserve the result through the **unified** critical persistence mechanism
- never silently treat as ordinary analytics degradation

Integration points (future wiring in M5): `N7OneShotHost` entry submit path, `LiveOMS` submit/cancel boundaries, YAML/`n7_operator_run` mutation gate — report via `ReportingPort` durable ack API in `writer.py`/`reporter.py`.

---

## 10. Closest-candidate semantics (frozen)

Among evaluations that passed eligibility, input-validity, and freshness and **reached executable edge evaluation**, retain highest **executable net-edge margin vs required threshold**. Record evaluation id, market/window, selected leg, executable net edge, required edge, signed margin, action, primary reason, relevant diagnostics. If none reached edge evaluation: explicit **no comparable candidate**. Top-K per stable primary reason via `closest_candidates_per_reason`.

---

## 11. Frozen `run_summary.json` structure

Top-level sections (fixed order/names):

```text
identity
status
configuration
data_health
strategy_evaluations
decisions_and_gates
closest_candidates
intent_and_risk
execution
positions_and_exits
performance
reporting_health
artifacts
```

| Section | Minimum contents |
|---------|------------------|
| `identity` | run id/name; strategy id + version; mode; market/window; start/end; git commit + dirty |
| `status` | lifecycle; terminal/partial/aborted; terminal reason; clean_shutdown bool |
| `configuration` | resolved config (or reference); config_hash; source YAML paths + overlays |
| `data_health` | source/freshness failures; invalid/missing inputs; skips due to data quality |
| `strategy_evaluations` | attempted, completed, skipped; detailed_persisted; detailed_sampled_dropped |
| `decisions_and_gates` | action counts; primary-reason distribution; all failed gates; gates not evaluated |
| `closest_candidates` | top-K by reason; edge-margin rule; or explicit absence |
| `intent_and_risk` | intents; risk allow/deny + reasons; plans created/rejected |
| `execution` | submissions; venue responses; orders/cancels/fills; qty/price/fees; failures |
| `positions_and_exits` | transitions; holds/exits/flatten; recon; residual exposure |
| `performance` | semantic label only: OBSERVE `observed_only` \| SHADOW `simulated` \| LIVE `real` — never mix simulated and real |
| `reporting_health` | health state + transitions; counters by lane; latest checkpoint; durable sequence; incompleteness reasons |
| `artifacts` | relative paths to manifest, audit, analytics, diagnostics, recordings, optional attachments |

Human-first file. Machine joins to JSONL via `artifacts` + `sequence`.

---

## 12. Reporting configuration (unchanged semantics)

Mandatory artifacts are never disableable booleans. Config controls optional analytics only.

**`full.yaml` (default):** per_evaluation/indicators/signals true; `debug_host_trace` false; `closest_candidates_per_reason: 5`; raw_recording false.

**`minimal.yaml`:** per_evaluation/indicators/signals false; debug false; closest K retained for summary; raw false; **aggregate counters still complete**.

Secrets never configurable; addresses not shortened by default. Writer internals stay code defaults. CLI `--reporting` defaults to `config/reporting/full.yaml` (files added in M6).

---

## 13. Filesystem layout

```text
var/
├── runs/
│   ├── <strategy_id>/<run_id>/
│   │   ├── manifest.json
│   │   ├── run_summary.json          # partial during run; final on clean shutdown
│   │   ├── audit_events.jsonl
│   │   ├── analytics_events.jsonl
│   │   ├── diagnostics/debug_events.jsonl   # optional
│   │   └── attachments/                     # optional, manifest-indexed
│   └── _ops/<check_or_tool_id>/<run_id>/    # operational checks (not strategy runs)
├── runtime_state/{shadow,live}/
├── recordings/<recording_or_dataset_id>/
└── logs/
```

Prefer preflight/seal/arm fields in **manifest + audit**. Use `attachments/` only for bounded artifacts that cannot reasonably embed (e.g. multi-file preflight package, large sealed JSON); always list in `manifest.attachments[]`.

Operational checks use manifest/summary/audit but set mode/`run_kind=ops` so they are not mistaken for strategy runs.

---

## 14. Runtime-state migration (`var/state` → `var/runtime_state`)

Approved; **no permanent alias**. Covers ack, residuals, policy mirror, shadow snapshots, durable live recovery. Procedure (M7): inspect → prepare → copy/atomic replace → verify → conflict detect → **refuse LIVE** on conflict → activate new paths → remove old active usage. Report cleanup must never touch `var/runtime_state/**` or `config/r7/**`.

---

## 15. Legacy dispositions (exact milestones)

Frozen ops convention: `var/runs/_ops/<check_or_tool_id>/<run_id>/`.

| Producer | Disposition | Destination | Delete milestone |
|----------|-------------|-------------|------------------|
| YAML observe/shadow + Observe/ShadowHost `JsonlFactSink` | Migrate | `var/runs/<strategy>/<run_id>/` | **M4** remove sink + primary `*_facts.jsonl` |
| YAML live + N7 session/compose/operator | Migrate | `var/runs/z_gap/<run_id>/` | **M5** remove stubs + primary `n7_oneshot_report.json` |
| `_capture_intents` stub | Replace | decision events via adapters + Z-Gap reporting | **M5** |
| `live_zgap_compose` observation stubs | Delete as analytics source | summary accumulators | **M5** |
| `n7_preflight` / sealed config | Migrate | audit/manifest; attachments if needed | **M5** |
| CLI `n7-live` / `tools/n7_live/*` | Migrate | same `var/runs/z_gap/<run_id>/` | **M5** (defaults); residual path strings **M8** verify |
| CLI `n7-preflight` / `n7-status` | Migrate | `_ops/n7_preflight/<id>/` or live run attachments when bound to a live run | **M5** |
| N1 raw market/PTB capture | Convert | `var/recordings/<recording_id>/` | **M7** |
| N2 connectivity smoke | Convert | `var/runs/_ops/n2_smoke/<run_id>/` | **M7** |
| N3 raw PTB/reference capture | Convert | `var/recordings/<recording_id>/` | **M7** |
| N3 validation/sealing/attestation report | Convert | `var/runs/_ops/n3_validation/<run_id>/` (or LIVE run audit/attachments when part of that run) | **M7** |
| N4/N5 standalone tools | Retire/delete | —; keep `N4ObserveRuntime` for N7 compose | **M7** |
| N6 tools + CLI `n6-*` | Retire/delete | — | **M7** |
| `r7b-live-once` / `r7b_live_once.py` | **Migrate** (not conditional) | `var/runs/<strategy_id>/<run_id>/` via common reporter; remove `facts_*.jsonl` / `var/reporting/r7b` | **M7** |
| CLI `observe`/`shadow` JSON | Migrate | `var/runs/…` via same hosts | **M4** |
| `live-preflight`, `r7a*`, `r7c-recon`, ack-regenerate reports | Migrate | `var/runs/_ops/<tool>/<id>/`; state → `runtime_state` | **M7** |
| CLI `live-once` (already refuses) | Delete dead surface | — | **M8** |
| `r7_paths` roots | Replace | `var/runs` + `var/runtime_state` | **M7** |
| Tests writing real `var/reporting` | Fix | `tmp_path` | **M7** |
| Docs stale paths | Update | Docs/latest + READMEs | **M8** (plus path updates already in M4–M7 as touched) |

Future product retirement of R7b is a **separate** decision; this plan **migrates** it.

---

## 16. Migration matrix (producers → one deletion milestone)

| Current | Consumer | New owner / destination | Migration | Deletion milestone | Replacement tests |
|---------|----------|-------------------------|-----------|--------------------|-------------------|
| Host `JsonlFactSink` | F3/F4/F5 | `RunReporter` + `writer.py` | Replace | **M4** | updated F3/F4/F5 |
| `FactEnvelope` host API | hosts | `contracts.py` envelope | Replace | **M4** (unused export cleanup **M8**) | contract tests |
| Z-Gap triple `extra_facts` primary | JSONL readers | `strategies/z_gap/reporting.py` | Diagnostics blob | **M4** stop mandatory triple | diagnostics asserts |
| `_capture_intents` stub | compose | adapters + Z-Gap reporting | Full decision emit | **M5** | fake LIVE analytics |
| `n7_oneshot_report.json` | operators | `run_summary.json` | Field move | **M5** | `test_yaml_live_run` |
| N7 tool `var/reporting/n7` defaults | tools | `var/runs/z_gap/…` | Path change | **M5** | tool path tests |
| N1 defaults | n1 tools | `var/recordings/…` | Relocate | **M7** | README/path |
| N2 defaults | n2 tools | `_ops/n2_smoke/…` | Relocate | **M7** | ops run shape |
| N3 raw vs report | n3 tools | recordings vs `_ops/n3_validation` | Split | **M7** | split path tests |
| N4/N5/N6 tools | operators | — | Retire | **M7** | docs absence |
| R7b facts/report under `var/reporting/r7b` | operators | common reporter `var/runs/…` | Migrate | **M7** | r7b report shape |
| `var/state/**` | recovery | `var/runtime_state/**` | Safe migrate §14 | **M7** | conflict refusal |
| Dead `live-once` CLI | none | — | Delete | **M8** | parser absence |

---

## 17. Milestones M1–M8

**Order:** diagnostics ownership (M3) before hosts (M4/M5).  
**Deletion:** same milestone as replacement. **M8** = residual verification only.  
**Rollback:** version-control revert only.

### Cutover ownership (frozen)

| Milestone | Path / producer cutover |
|-----------|-------------------------|
| **M4** | OBSERVE/SHADOW → `var/runs/<strategy_id>/<run_id>/`; remove `JsonlFactSink` + legacy primary outputs + obsolete tests; **capture sync-writer performance baseline before removal** |
| **M5** | LIVE/N7 → same `var/runs/<strategy_id>/<run_id>/`; fix `_capture_intents`; remove stubs + primary `n7_oneshot_report.json` after mandatory fields migrated; wire pre-mutation barrier + health states |
| **M7** | `var/state` → `var/runtime_state`; N1–N3/R7b/ops paths; retire N4–N6 tools; test isolation; remove abandoned-directory active writes |
| **M8** | Verify/remove **accidental residuals only** — not first deletion of M4/M5/M7 producers |

---

### M1 — Contracts and compact package

| | |
|--|--|
| **Objective** | Compact package: envelope, health enums, registration Protocol, ReportingPort surface — no host change |
| **Create** | `contracts.py`, `reporter.py` skeleton, `writer.py` skeleton, `summary.py` (accumulator types), `config.py` types, `paths.py`, `adapters.py` stubs; revise `__init__.py` |
| **Modify/Delete** | none in hosts |
| **Integration** | import only |
| **Behavior** | validated contracts + `HEALTHY`/`DEGRADED_ANALYTICS`/`CRITICAL_AUDIT_FAILURE` |
| **Legacy removed** | none |
| **Tests** | envelope/lineage/health unit tests |
| **Acceptance** | no output path change |
| **Depends** | — |
| **Rollback** | revert M1 |

---

### M2 — Lifecycle, writer, accumulators, checkpoints

| | |
|--|--|
| **Objective** | Synthetic runs: two lanes, accumulators-before-sample, atomic summary checkpoints, finalize |
| **Complete** | `reporter.py`, `writer.py`, `summary.py`, `paths.py` |
| **Behavior** | partial checkpoint while `RUNNING`; final summary on clean shutdown; durable seq ack API for critical events |
| **Legacy removed** | none |
| **Tests** | analytics drop cannot change counters; critical ack; checkpoint readable after simulated crash; stale RUNNING classification helper |
| **Acceptance** | offline artifact tree + partial/final summary semantics |
| **Depends** | M1 |
| **Rollback** | revert M2 |

---

### M3 — Strategy contract + Z-Gap diagnostics builder

| | |
|--|--|
| **Objective** | Z-Gap diagnostics owner before host wiring; developer checklist docs |
| **Create** | `strategies/z_gap/reporting.py`; docs section in `extending_the_framework.md` |
| **Modify** | binding may call builder (no math change); composition passes registration into reporter |
| **Delete** | none of host writers |
| **Behavior** | namespaced diagnostics + gate records + closest fields from existing valuations |
| **Tests** | schema validation; second fake strategy via composition; no z_gap switches in reporting core |
| **Acceptance** | fixture eval → complete diagnostics without host I/O |
| **Depends** | M1–M2 |
| **Rollback** | revert M3 |

---

### M4 — OBSERVE + SHADOW

| | |
|--|--|
| **Objective** | Hosts use ReportingPort; switch outputs to `var/runs/<strategy_id>/<run_id>/`; remove JsonlFactSink same milestone |
| **Modify** | `observe_host.py` (`_emit`, `evaluate_once`), `shadow_host.py`, `adapt.output_path_for_run`, JSON observe/shadow CLI outs, F3/F4/F5 |
| **Delete** | host `JsonlFactSink` construction; primary `*_facts.jsonl` expectations |
| **Baseline** | measure current sync `JsonlFactSink` overhead **before** deletion |
| **Behavior** | full strategy explanation; SHADOW critical lifecycle; simulation assumptions in `performance.simulated` |
| **Tests** | F3/F4/F5 on new layout; tmp_path CLI; counters vs persisted rows |
| **Acceptance** | no `JsonlFactSink(` in observe/shadow hosts; artifacts under `var/runs/…` |
| **Depends** | M1–M3 |
| **Rollback** | revert M4 |

---

### M5 — LIVE/N7 + `_capture_intents` + barrier

| | |
|--|--|
| **Objective** | LIVE parity; `var/runs/z_gap/<run_id>/`; pre-mutation barrier; remove stubs and primary oneshot report |
| **Modify** | `n7_live_session._capture_intents`, `live_zgap_compose`, `n7_operator_run`, `n7_preflight`, `live_run`, `n7_bridge`, LiveOMS emit, N7 tool defaults, live tests |
| **Delete** | class-name stub path; observation stub analytics; `_write_report` primary `n7_oneshot_report.json` **after** fields live in summary/manifest/audit |
| **Behavior** | decision+diagnostics every eval; health states; exposure-increasing mutation only after critical durable ack; post-mutation persist failure → `CRITICAL_AUDIT_FAILURE` |
| **Tests** | fake LIVE histograms; barrier unit/fake tests; cancel/flatten still allowed under critical failure |
| **Acceptance** | LIVE strategy explanation parity; mandatory ops in summary; no primary oneshot filename |
| **Depends** | M1–M4 |
| **Rollback** | revert M5 |

---

### M6 — Reporting YAML + CLI

| | |
|--|--|
| **Objective** | `config/reporting/{full,minimal}.yaml`; `--reporting`; default full |
| **Create** | those YAML files |
| **Modify** | `cli.py`, `config.py`, how-to docs |
| **Tests** | minimal vs full cardinality; reject disabling mandatory evidence |
| **Depends** | M1–M5 |
| **Rollback** | revert M6 |

---

### M7 — State migration, tools, test isolation

| | |
|--|--|
| **Objective** | runtime_state migration; N1 recordings; N2/N3/_ops paths; migrate R7b; retire N4–N6; isolate tests; purge abandoned active writes |
| **Create** | migration procedure/helper for §14 |
| **Modify** | `r7_paths.py`, execution persistence defaults, N1–N3/R7b/ops CLIs, `test_yaml_run_cli.py`, READMEs |
| **Delete** | `var/reporting`/`var/state` active defaults; N4/N5/N6 tool entry points; R7b legacy fact writer |
| **Tests** | state conflict refuses LIVE; path tests; no repo `var/` pollution from pytest |
| **Depends** | M1–M6 |
| **Rollback** | revert M7 (+ restore state copies if needed) |

---

### M8 — Residual verification only

| | |
|--|--|
| **Objective** | Prove one reporter; remove accidental leftovers only |
| **Delete** | dead `live-once` surface; unused old reporting exports; stale docs refs; stray `var/reporting` strings in active code |
| **Must not** | be first deletion point for producers already replaced in M4/M5/M7 |
| **Tests** | ripgrep gates for JsonlFactSink hosts, stubs, oneshot primary, abandoned dirs |
| **Acceptance** | checklist §18 |
| **Depends** | M1–M7 |
| **Rollback** | revert M8 |

---

## 18. Final clean-cutover checklist

- [x] One active ReportingPort/RunReporter for OBSERVE, SHADOW, LIVE
- [x] No dual write / feature flag / compatibility / legacy fallback
- [x] No `_capture_intents` stub; no observation stub analytics
- [x] No mode-specific parallel reporter
- [x] No active host `JsonlFactSink`; no primary `*_facts.jsonl`
- [x] No primary `n7_oneshot_report.json`
- [x] No production/test writes to abandoned dirs
- [x] No Z-Gap field logic inside generic reporting package
- [x] N4–N6 tools retired; N1→recordings; N2→`_ops/n2_smoke`; N3 split recordings vs `_ops/n3_validation`
- [x] R7b migrated to common reporter under `var/runs/`
- [x] Runtime state under `var/runtime_state/` only (+ `config/r7`)
- [x] Summary counters correct under analytics sampling; checkpoints cover hard crash honestly
- [x] Pre-mutation barrier on every exposure-increasing LIVE mutation
- [x] Health states `HEALTHY` / `DEGRADED_ANALYTICS` / `CRITICAL_AUDIT_FAILURE` used consistently

---

## 19. Performance validation

1. **Before M4 deletes sync path:** measure baseline `JsonlFactSink` / host emit overhead on fixture OBSERVE.  
2. After M4/M5: measure new reporter overhead on same fixtures.  
3. Define acceptance **from measured baselines** only — no invented millisecond targets in this plan.  
4. Validate critical-lane ack latency only as relative/functional acceptance for the pre-mutation barrier.  
5. Choose checkpoint interval from observed eval rates and I/O cost during validation.

---

## 20. Documentation changes

| Doc | When |
|-----|------|
| `extending_the_framework.md` (+ optional strategy-reporting page) | M3 |
| `reporting_and_operations.md`, run_modes, quickstart | as paths land (M4–M7), finalize M8 |
| Tool READMEs | M5 (N7), M7 (N1–N6, R7b) |
| This plan | authoritative |

---

## 21. Remaining notes / blockers

**Resolved in this revision:** checkpointing; pre-mutation barrier; health states; schema table; Z-Gap emit map; frozen summary shape; exact M4/M5/M7/M8 cutovers; N1/N2/N3 classification; unconditional R7b migrate; Naming (`ReportingPort`, `SummaryAccumulators` in `summary.py`); composition-time registration.

**Genuine planning blockers:** none.

**Non-blocking implementation detail:** exact stale-`RUNNING` age/heartbeat rule for crash classification — define during M2 validation, document in code comments/tests.

---

## 22. Final repository tree

```text
src/tyrex_pm/reporting/{__init__,contracts,reporter,writer,summary,config,paths,adapters}.py
src/tyrex_pm/strategies/z_gap/reporting.py
config/reporting/{full,minimal}.yaml
config/r7/acknowledgment_policy.json

var/
  runs/<strategy_id>/<run_id>/{manifest,run_summary,audit_events,analytics_events,diagnostics?,attachments?}
  runs/_ops/<check_or_tool_id>/<run_id>/…
  runtime_state/{shadow,live}/…
  recordings/<id>/…
  logs/…
```

---

## 23. Plan non-goals

Does not authorize implementation, YAML creation, code deletion, state migration, `var/` cleanup, strategy/risk/execution changes, LIVE arming, or venue mutations.

---

**Readiness:** Implementation-ready pending explicit owner authorization to begin M1.
