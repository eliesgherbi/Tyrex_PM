# Work breakdown: reporting and observability (executable plan)

**Parent architecture:** [`plan_reporting_observability.md`](plan_reporting_observability.md)  
**Purpose:** Implementation task inventory with dependencies, acceptance criteria, and sequencing—not a restatement of architecture.

---

## A. Current-state reconciliation (codebase vs plan)

### A.1 Already partially supported by code

| Capability | Evidence in repo |
|------------|------------------|
| **Stable guru trace id** | `GuruTradeSignal.source_trade_id` → `OrderIntent.correlation_id` (`core/types.py`, `copy_strategy.py`). |
| **Deterministic framework COID** | `_client_order_id_from_guru_correlation` SHA256→`TX{26hex}` (`execution/nautilus_guru_exec.py`). |
| **Guru order tagging** | `tags=[_guru_tag(correlation_id)]` on submit (`nautilus_guru_exec.py`); B3 identity prefers `guru_cid=` then COID pattern (`state_readers.is_guru_resting_order`). |
| **Ingress signal logging** | `GuruSignalPipeline.try_publish` logs `guru_signal_emitted` with `correlation_id`, timings (`data/guru_ingest_pipeline.py`). |
| **Shadow stream compare** | `guru_stream_would_emit` with `correlation_id`, `would_publish_new` (`guru_stream_actor.py`). |
| **Strategy branch logging** | `copy_skip`, `shadow_order_intent`, `live_order_intent` with latencies (`copy_strategy.py`). |
| **Risk deny logging** | `tyrex_risk_ops` + gate fields (`risk/configured.py`); strategy logs `risk_denied` + `risk_detail`. |
| **Execution submit/skip logging** | `LIVE_ORDER_SUBMIT`, C3 skips, `LIVE_ORDER_ERROR`, timeout cancel (`nautilus_guru_exec.py`). |
| **Legacy HTTP submit** | `PolymarketExecutionPolicy` logs truncated response + extracted `orderID` (`polymarket_policy.py`). |
| **Reader surfaces for capital/exposure** | `OrderSnapshot`, `NautilusExecutionStateReader`, `NautilusPortfolioExposureAggregator`, `AccountSnapshot`, `AllowanceSnapshot` (`state_readers.py`, `portfolio_exposure.py`). |
| **Compose injects readers into risk** | `build_guru_trading_node` (`guru_compose.py`) wires `exec_reader`, `portfolio_agg`, `position_reader`, etc., per `use_nautilus` / `use_framework_submit`. |

### A.2 Available today only as unstructured logs (not canonical dataset)

| Information | Source |
|-------------|--------|
| Full strategy/sizing numbers on **accept** | `copy_conviction_diag` is **DEBUG** only; accept path has no INFO fact equivalent. |
| **Risk allow** | No log line on success; only denies emit `tyrex_risk_ops` or strategy never reaches intent. |
| **Normalization/book internals** | C3 decisions mostly in log strings, not typed payloads. |
| **Order lifecycle / fills** | `CopyStrategy.on_order_event` only forwards to `notify_order_event` for timer cancel—**no recording**. |
| **`guru_primary_report` / `guru_shadow_report`** | Regex over `run_nautilus.log`—not structured run store. |

### A.3 Visible Nautilus/framework state but not yet exposed to reporting

| Surface | Module | Reporting gap |
|---------|--------|----------------|
| Open orders | `Cache.order`, `NautilusExecutionStateReader.list_open_orders` / snapshots | Not persisted as time series. |
| Order events | `OrderEvent` subclasses (fills, accepts, rejects) | Not subscribed for facts in Tyrex. |
| Portfolio account | `NautilusAccountSnapshotProvider` | Snapshots taken for risk gates, not written as `account_snapshot_fact`. |
| Allowance / balance | `ClobAllowanceStateProvider`, `_capital_gate_eval` | Same—ephemeral unless logged on deny. |
| B1 aggregate | `NautilusPortfolioExposureAggregator.aggregate` | Returned to `evaluate`, not stored per evaluation. |
| Marks / prices | `cache_best_mark_float`, `Cache.price` | Needed for unrealized/slippage baselines—exist but not recorded. |

### A.4 Requires new Tyrex instrumentation (not adapter-only)

- `FactRecorder` / `RunContext` injection from `run_guru.py` → `guru_compose.py` → strategy, risk policy, execution ports, optionally actors.
- Explicit **strategy_decision_fact** (skip+accept), **sizing_fact**, **risk_decision_fact** (allow+deny), **execution_intent_fact**, **normalization_fact**, **book_constraint_fact**, **execution_outcome_fact**.
- **Order/fill fan-out** from `CopyStrategy.on_order_event` (or equivalent) into lifecycle + fill facts.
- **Snapshot tick** or hook after `aggregate()`, `snapshot()`, on deny/submit triggers for exposure/account facts.

### A.5 Adapter / runtime dependencies (may block completeness)

| Topic | Reality check (`state_readers.py`, `current_state.md`) |
|-------|--------------------------------------------------------|
| **Tag preservation on orders** | Docs acknowledge **Tier 3 COID fallback** if tags stripped—joins remain possible but **weaker**; reporting must record `data_quality.tags_missing` when detected. |
| **`venue_order_id` timing** | May be `None` on `OrderSnapshot` until venue ack—plan’s nullable spine on `client_order_id` is **correct**. |
| **Legacy py-clob path** | **No** Nautilus `submit_order` for guru orders—**OrderEvent stream may be empty or partial** for those orders; HTTP response gives external id; **fill reconciliation** may require polling or separate user-stream integration not visible in `polymarket_policy.py`. |
| **`portfolio_agg` / `position_reader` null** | When `not (use_nautilus and use_framework_submit)`—B0 contract limits portfolio cap; exposure facts must reflect **wiring absent** vs **incomplete snapshot**. |
| **Unrealized PnL** | Depends on marks feeding `Portfolio`/`Cache` as documented in `portfolio_exposure.py`—same operational caveats as Phase B validation. |

### A.6 Identifier/join reliability

| Join | Reliability |
|------|-------------|
| `correlation_id` ↔ guru signal | **High**—single writer id. |
| `correlation_id` ↔ `client_order_id` | **High** on framework path (deterministic function). |
| `correlation_id` ↔ lifecycle events | **High** if events include `client_order_id` and we map COID→correlation via inverse hash or **store mapping fact at submit**. (**Recommended:** emit `order_correlation_map_fact` at submit with both ids—avoids reverse-hash dependency.) |
| `guru_cid` tag ↔ correlation | **Medium**—preferred when tags present; **degraded** when only COID pattern. |
| Legacy HTTP `orderID` ↔ Nautilus | **Low**—different id namespace; needs explicit `venue_order_id` / external id on `execution_outcome_fact` and lifecycle stub from HTTP response. |

### A.7 Plan assumptions to correct

1. **`market_id` on guru benchmark:** `GuruTradeSignal` has no `market_id` field today—either **null** in v1 facts or **Task** to enrich via resolver/Gamma (separate data task).  
2. **`guru_trade_benchmark_fact` duplicate of `guru_signal_fact`:** Collapse to **one** physical table with `fact_subtype` unless query perf requires denorm view—implementation choice, not two write paths.  
3. **“Every OrderEvent” for legacy:** May be **infeasible** without adapter work—WBS separates **framework order-event spine** vs **legacy HTTP outcome facts**.  
4. **`position_fact` from Nautilus:** Need explicit subscription to position/portfolio events or periodic `Portfolio` scrape—code today does not centralize position change callbacks in Tyrex.

---

## B. Work breakdown plan

### B.1 Schema tasks

#### SCH-01 — Canonical ID and join contract (document + types)

| Field | Content |
|-------|---------|
| **Task ID** | SCH-01 |
| **Task name** | Canonical identifiers and join rules (frozen contract) |
| **Objective** | Lock `run_id`, `correlation_id`, `client_order_id`, `venue_order_id`, snapshot seq ids, and required foreign keys per fact type. |
| **Scope** | Author `Docs/Implementation/reporting_schema.md` (or `src/tyrex_pm/reporting/schema/CONTRACT.md`) + Python `TypedDict`/`dataclass` stubs for validators. |
| **Affected modules/files** | New: `src/tyrex_pm/reporting/schema/ids.py`, `…/joins.md`; reference `plan_reporting_observability.md` §3. |
| **Dependencies** | None. |
| **Deliverables** | Join matrix table; rule for `order_correlation_map_fact` (recommended); nullability rules for late `venue_order_id`. |
| **Acceptance criteria** | Review sign-off: every fact type in §B.3 lists mandatory ids; engineers can implement emitters without ambiguity. |
| **Risks/notes** | Legacy id space called out explicitly. |

#### SCH-02 — Fact type schemas (JSON Schema or Pydantic)

| Field | Content |
|-------|---------|
| **Task ID** | SCH-02 |
| **Task name** | Per-fact JSON schemas v1 |
| **Objective** | Machine-validatable payloads for all fact kinds in parent plan §4 + `order_correlation_map_fact`. |
| **Scope** | One schema file per fact family or unified `facts.json` defs with `discriminator: fact_type`. |
| **Affected modules/files** | `src/tyrex_pm/reporting/schema/facts_v1.py`, `schemas/*.json` optional. |
| **Dependencies** | SCH-01. |
| **Deliverables** | Validation helper `validate_fact(dict) -> None` raising structured errors. |
| **Acceptance criteria** | Golden fixtures pass validation; unknown `fact_type` rejected; `fact_schema_version` required on each row. |
| **Risks/notes** | Keep optional fields explicit `nullable` in schema. |

#### SCH-03 — `delta_reason_code` taxonomy enum

| Field | Content |
|-------|---------|
| **Task ID** | SCH-03 |
| **Task name** | Delta reason taxonomy + mapping from `ReasonCode` / gates |
| **Objective** | Stable enum for guru-vs-us and lost-notional rollups; map Tyrex `ReasonCode` + `gate=` strings. |
| **Scope** | `src/tyrex_pm/reporting/taxonomy.py` (+ table in doc). |
| **Affected modules/files** | `core/reason_codes.py` (import-only from reporting side to avoid cycles); mapping dict. |
| **Dependencies** | SCH-02 (field appears in summary schema). |
| **Deliverables** | `to_delta_reason(reason_code: str, gate: str | None) -> str` |
| **Acceptance criteria** | Every `ReasonCode` used in guru path maps or defaults to `unknown` with **lint test** for unmapped new codes. |
| **Risks/notes** | Extend when new reasons added—CI gate. |

#### SCH-04 — Summary and rollup output schemas

| Field | Content |
|-------|---------|
| **Task ID** | SCH-04 |
| **Task name** | `summary.json` v1 schema |
| **Objective** | Sections: run_overview, strategy_behavior, guru_vs_us, execution_quality, capital_deployment, risk_impact, anomalies, token_breakdown, config_fingerprint, pipeline_health, data_quality_flags. |
| **Scope** | JSON Schema + version field `summary_schema_version`. |
| **Affected modules/files** | `src/tyrex_pm/reporting/summary_schema_v1.py` |
| **Dependencies** | SCH-02, SCH-03. |
| **Deliverables** | Example `summary.json` from fixture run. |
| **Acceptance criteria** | Validator in CI; breaking change bumps version. |
| **Risks/notes** | Numeric fields use explicit units (USD, ms). |

#### SCH-05 — Data quality / completeness flags

| Field | Content |
|-------|---------|
| **Task ID** | SCH-05 |
| **Task name** | Run-level and per-section `data_quality` model |
| **Objective** | Flags: `run_ended_cleanly`, `facts_incomplete`, `order_events_sparse`, `legacy_path_no_events`, `tags_missing_rate`, `unrealized_pnl_unavailable_reason`, etc. |
| **Scope** | Enum + inclusion in `run_manifest` + `summary.json`. |
| **Affected modules/files** | `reporting/schema/data_quality.py`, recorder shutdown hook. |
| **Dependencies** | SCH-04. |
| **Deliverables** | Documented flag meanings for UI. |
| **Acceptance criteria** | Fixture simulating crash sets `run_ended_cleanly=false`; legacy live sets `order_events_sparse` when no events captured. |
| **Risks/notes** | Avoid silent “green” summaries when pipeline failed. |

#### SCH-06 — Versioning and migration rules

| Field | Content |
|-------|---------|
| **Task ID** | SCH-06 |
| **Task name** | Schema migration policy |
| **Objective** | Rule: additive minor, breaking major; ETL `tyrex_report migrate` or versioned readers; retention paths. |
| **Scope** | Short doc + version constants `REPORTING_FACT_SCHEMA_MAJOR`. |
| **Affected modules/files** | `src/tyrex_pm/reporting/versioning.py` |
| **Dependencies** | SCH-02. |
| **Deliverables** | README section for operators. |
| **Acceptance criteria** | Old fixture still loads read-only after one simulated breaking change (test). |
| **Risks/notes** | SQLite DDL migrations tracked per `run.sqlite` build tool version. |

---

### B.2 Recorder / storage tasks

#### REC-01 — RunContext + directory layout

| Field | Content |
|-------|---------|
| **Task ID** | REC-01 |
| **Task name** | `RunContext` (run_id, paths, strategy metadata) |
| **Objective** | Single object passed to compose: `var/reporting/runs/<run_id>/manifest.json`, `facts.jsonl`, `logs_pointer`. |
| **Scope** | Create dirs at start; write initial manifest. |
| **Affected modules/files** | `src/tyrex_pm/reporting/context.py`, `scripts/run_guru.py`. |
| **Dependencies** | SCH-01. |
| **Deliverables** | `RunContext.from_cli(...)` |
| **Acceptance criteria** | Second run does not clobber first; path printed to stdout. |
| **Risks/notes** | Windows path sanity (`guru_run_logging.py` patterns). |

#### REC-02 — FactRecorder interface + null recorder

| Field | Content |
|-------|---------|
| **Task ID** | REC-02 |
| **Task name** | `FactRecorder` protocol + `NoOpFactRecorder` |
| **Objective** | `record(fact_type, payload)`; tests use no-op; production uses async sink. |
| **Scope** | No trading imports in core protocol. |
| **Affected modules/files** | `src/tyrex_pm/reporting/recorder.py` |
| **Dependencies** | SCH-02. |
| **Deliverables** | Protocol used in unit tests for strategy without disk. |
| **Acceptance criteria** | All emitters accept `FactRecorder | None` defaulting to no-op when reporting disabled. |
| **Risks/notes** | Optional: typed methods `record_guru_signal(...)` wrapping dict builder. |

#### REC-03 — Bounded queue + writer thread + JSONL sink

| Field | Content |
|-------|---------|
| **Task ID** | REC-03 |
| **Task name** | Batched JSONL append sink |
| **Objective** | Non-blocking enqueue; flush batches; periodic fsync policy; shutdown flush. |
| **Scope** | Config: `max_queue`, `batch_size`, `flush_interval_ms`. |
| **Affected modules/files** | `src/tyrex_pm/reporting/sinks/jsonl.py`, `runtime` shutdown registration. |
| **Dependencies** | REC-01, REC-02. |
| **Deliverables** | `JsonlFactSink` |
| **Acceptance criteria** | Stress test: 10k facts without blocking strategy thread >1ms avg (heuristic benchmark); crash leaves valid JSONL lines (no torn writes mid-line). |
| **Risks/notes** | High-water mark: **backpressure policy** = block producer briefly then emit `report_pipeline_health_fact`—document choice. |

#### REC-04 — `report_pipeline_health_fact` emission

| Field | Content |
|-------|---------|
| **Task ID** | REC-04 |
| **Task name** | Pipeline self-telemetry |
| **Objective** | Queue depth max, flush errors, dropped facts (if any), final flush ok/fail. |
| **Scope** | Writer owns counters; `atexit` / `node.stop` hook. |
| **Affected modules/files** | `reporting/sinks/jsonl.py`, `run_guru.py` or compose. |
| **Dependencies** | REC-03. |
| **Deliverables** | At least one health row per run end. |
| **Acceptance criteria** | Simulated disk full → health fact + log + trading continues. |
| **Risks/notes** | Health fact may be last line only if prior drops—acceptable. |

#### REC-05 — Post-run `run.sqlite` builder

| Field | Content |
|-------|---------|
| **Task ID** | REC-05 |
| **Task name** | ETL: `facts.jsonl` → `run.sqlite` |
| **Objective** | Typed tables mirroring fact types; indexes on `correlation_id`, `client_order_id`, `ts`, `token_id`. |
| **Scope** | CLI `python -m tyrex_pm.reporting build_db --run-dir …` |
| **Affected modules/files** | `src/tyrex_pm/reporting/etl/jsonl_to_sqlite.py` |
| **Dependencies** | SCH-02, REC-03. |
| **Deliverables** | `run.sqlite` with pragma journal_mode=WAL optional. |
| **Acceptance criteria** | Round-trip: row counts match JSONL; join smoke query `signal→intent→lifecycle`. |
| **Risks/notes** | Large blobs (config_snapshot) in single row OK. |

#### REC-06 — Config snapshot serialization

| Field | Content |
|-------|---------|
| **Task ID** | REC-06 |
| **Task name** | `config_snapshot` fact builder |
| **Objective** | Normalize `StrategySettings`, `RiskSettings`, `RuntimeSettings` + material env vars list into deterministic JSON (sorted keys) + `sha256`. |
| **Scope** | New `reporting/config_capture.py`; no secrets from `.env` values, only **names** or redacted hashes if needed. |
| **Affected modules/files** | `config/loaders.py` (expose `to_public_dict` on settings dataclasses) |
| **Dependencies** | REC-02. |
| **Deliverables** | One `config_snapshot` row at run start. |
| **Acceptance criteria** | Two identical YAML loads → identical hash; changing one YAML field changes hash. |
| **Risks/notes** | Document which env vars are “material” for behavior. |

#### REC-07 — Crash / incomplete run handling

| Field | Content |
|-------|---------|
| **Task ID** | REC-07 |
| **Task name** | Manifest finalization + incomplete semantics |
| **Objective** | `ended_at_utc` null on SIGINT/crash; `data_quality.run_incomplete=true`; best-effort flush. |
| **Scope** | Signal handlers optional (Windows nuance)—minimum: `finally` in `run_guru.py`. |
| **Affected modules/files** | `scripts/run_guru.py`, `reporting/context.py` |
| **Dependencies** | REC-01, REC-05, SCH-05. |
| **Deliverables** | Documented behavior in OPERATIONS. |
| **Acceptance criteria** | Kill process mid-run → manifest shows incomplete; JSONL parseable up to last full line. |
| **Risks/notes** | Do not corrupt JSONL on interrupt. |

---

### B.3 Integration tasks by module

#### INT-RUN-01 — Entrypoint: `run_id` + RunContext + recorder binding

| Field | Content |
|-------|---------|
| **Task ID** | INT-RUN-01 |
| **Task name** | Wire reporting at process start |
| **Objective** | Generate `run_id` (ULID/UUID); build `RunContext`; construct `JsonlFactSink` when `runtime.reporting_enabled` (new field). |
| **Facts** | `run_manifest` (partial), `config_snapshot`; end: finalize manifest. |
| **Current** | None. |
| **Change** | `load_runtime_settings` validates flag; `build_guru_trading_node(..., run_context=...)`. |
| **IDs** | `run_id` on every fact. |
| **Affected** | `scripts/run_guru.py`, `config/loaders.py`, `RuntimeSettings`, `CONFIG_MODEL.md`, `live_polymarket.yaml` comment. |
| **Dependencies** | REC-01–03, REC-06, SCH-02. |
| **Acceptance** | One run produces `var/reporting/runs/<id>/` with manifest + jsonl. |

#### INT-CMP-01 — Compose passes recorder to components

| Field | Content |
|-------|---------|
| **Task ID** | INT-CMP-01 |
| **Task name** | `build_guru_trading_node` injection |
| **Objective** | Pass `FactRecorder` into `CopyStrategy`, `ConfiguredRiskPolicy`, execution port, guru actors. |
| **Facts** | None by itself; enables downstream. |
| **Current** | No recorder parameter. |
| **Change** | Optional kwargs; strategy stores `_recorder`; risk stores weakref or direct ref (avoid cycles). |
| **Affected** | `runtime/guru_compose.py` |
| **Dependencies** | INT-RUN-01, REC-02. |
| **Acceptance** | Unit test: mock recorder receives fact when strategy skips (stub). |

#### INT-ING-01 — `guru_ingest_pipeline.py`

| Field | Content |
|-------|---------|
| **Task ID** | INT-ING-01 |
| **Task name** | `guru_signal_fact` on publish |
| **Objective** | Structured duplicate of `guru_signal_emitted` fields + `fact_schema_version`. |
| **Current** | Log only. |
| **Change** | `GuruSignalPipeline.__init__` accepts optional `FactRecorder`; `try_publish` records. |
| **IDs** | `correlation_id`, `run_id`. |
| **Affected** | `data/guru_ingest_pipeline.py`, `guru_monitor.py`, `guru_stream_actor.py` (pipeline ctor sites). |
| **Dependencies** | INT-CMP-01. |
| **Acceptance** | Integration test: one emitted signal → one fact row. |

#### INT-ING-02 — `guru_stream_actor.py` shadow / health

| Field | Content |
|-------|---------|
| **Task ID** | INT-ING-02 |
| **Task name** | `guru_stream_would_emit` + fallback facts |
| **Objective** | `health_anomaly_fact` / dedicated `guru_shadow_compare_fact` for would_emit lines; fallback activation/clear. |
| **Current** | Logs. |
| **Change** | Recorder optional on actor. |
| **Affected** | `data/guru_stream_actor.py` |
| **Dependencies** | INT-CMP-01. |
| **Acceptance** | Shadow run yields would_emit facts count == log line count for test payload. |

#### INT-ING-03 — `guru_monitor.py` / `data_api_client.py`

| Field | Content |
|-------|---------|
| **Task ID** | INT-ING-03 |
| **Task name** | Poll errors, backoff |
| **Objective** | `health_anomaly_fact` for `guru_poll_error`, `poller_backoff`. |
| **Current** | Logs. |
| **Change** | Inject recorder into actor or pipeline only (monitor already has `log.info`). |
| **Affected** | `data/guru_monitor.py`, `data/data_api_client.py` |
| **Dependencies** | INT-CMP-01. |
| **Acceptance** | Simulated HTTP error → health fact with `detail` truncated. |

#### INT-ING-04 — `guru_gap_fill.py`

| Field | Content |
|-------|---------|
| **Task ID** | INT-ING-04 |
| **Task name** | Gap fill summary facts |
| **Objective** | Rows/published/ts; errors. |
| **Current** | Logs. |
| **Affected** | `data/guru_gap_fill.py` |
| **Dependencies** | INT-ING-01 (shared recorder plumbing). |
| **Acceptance** | Matches `guru_primary_report.py` counts for gap fill in fixture log parity test. |

#### INT-STR-01 — `copy_strategy.py` decisions + sizing + intent

| Field | Content |
|-------|---------|
| **Task ID** | INT-STR-01 |
| **Task name** | Strategy facts for all branches |
| **Objective** | `strategy_decision_fact` every evaluation; `sizing_fact` on accept after `size()`; `execution_intent_fact` after risk approve; latencies in ms fields. |
| **Current** | Logs; conviction DEBUG only. |
| **Change** | `CopyStrategyConfig` or ctor gains `recorder`; `_handle_branch` emits. |
| **IDs** | `correlation_id`, `run_id`. |
| **Affected** | `strategy/copy_strategy.py` |
| **Dependencies** | INT-CMP-01, SCH-02. |
| **Acceptance** | For each `copy_skip` log there exists matching `strategy_decision_fact` OR dedicated skip subtype with same reason. |

#### INT-RSK-01 — `risk/configured.py` allows + denies

| Field | Content |
|-------|---------|
| **Task ID** | INT-RSK-01 |
| **Task name** | `risk_decision_fact` on every `evaluate` return |
| **Objective** | Deny: existing log fields + structured; **Allow**: explicit with `gate=all_passed` or per-gate simplified summary + `intent_notional`, `e_portfolio` snapshot when B1 ran. |
| **Current** | Deny logs only. |
| **Change** | Optional `FactRecorder` on `ConfiguredRiskPolicy`; `evaluate` tail records. |
| **IDs** | `correlation_id`. |
| **Affected** | `risk/configured.py` |
| **Dependencies** | INT-CMP-01. |
| **Acceptance** | Approve path produces risk fact without tyrex_risk_ops log—dataset still complete. |

#### INT-RSK-02 — Exposure snapshot on material events

| Field | Content |
|-------|---------|
| **Task ID** | INT-RSK-02 |
| **Task name** | `exposure_fact` / `portfolio_snapshot_fact` |
| **Objective** | After `aggregate()` inside deny **or** allow path (configurable throttle); include `complete`, `e_portfolio`, legs, `omitted_instruments_*`. |
| **Current** | Not persisted. |
| **Change** | Call `portfolio_exposure.aggregate` results into recorder when `portfolio_agg` not None. |
| **Affected** | `risk/configured.py` |
| **Dependencies** | INT-RSK-01. |
| **Acceptance** | Deny for B2 cap includes numeric fields matching log line within epsilon. |

#### INT-EXE-01 — `nautilus_guru_exec.py`

| Field | Content |
|-------|---------|
| **Task ID** | INT-EXE-01 |
| **Task name** | Normalization, book, outcome facts |
| **Objective** | `normalization_fact`, `book_constraint_fact` for each C3 branch; `execution_outcome_fact` submit/skip/error; **`order_correlation_map_fact`** after COID minted. |
| **Current** | Logs. |
| **Change** | Port holds recorder ref; refactor `_c3_shape_prepare` to return structured DTO for facts + logging. |
| **IDs** | `correlation_id`, `client_order_id`, `instrument_id`. |
| **Affected** | `execution/nautilus_guru_exec.py` |
| **Dependencies** | INT-CMP-01. |
| **Acceptance** | One skip (`EXEC_ENTRY_GUARD_SKIP`) produces outcome fact without submit. |

#### INT-EXE-02 — `polymarket_policy.py` legacy

| Field | Content |
|-------|---------|
| **Task ID** | INT-EXE-02 |
| **Task name** | Legacy execution_outcome + external order id |
| **Objective** | On HTTP success: `execution_outcome_fact` with `venue_order_id`/`external_order_id`; map row for reconciliation. |
| **Current** | Log prefix only. |
| **Change** | Constructor takes optional recorder; capture **full id** in fact (not truncated). |
| **Affected** | `execution/polymarket_policy.py` |
| **Dependencies** | INT-CMP-01. |
| **Acceptance** | Response dict id == fact field (unit test with mock client). |

#### INT-ORD-01 — Order event dispatch

| Field | Content |
|-------|---------|
| **Task ID** | INT-ORD-01 |
| **Task name** | `order_lifecycle_fact` + `fill_fact` from `OrderEvent` |
| **Objective** | In `CopyStrategy.on_order_event`, fan-out to `ReportingOrderHandler` mapping event type → facts; preserve `notify_order_event` for C3 timer. |
| **Current** | Timer only. |
| **Change** | `src/tyrex_pm/reporting/order_events.py` with Nautilus imports isolated here. |
| **IDs** | `client_order_id` + lookup `correlation_id` from map fact or COID heuristic. |
| **Affected** | `strategy/copy_strategy.py`, new `reporting/order_events.py` |
| **Dependencies** | INT-EXE-01 (map fact), SCH-02. |
| **Acceptance** | Mock `OrderFilled` produces fill_fact + lifecycle transition in fixture. |

#### INT-ST-01 — Account + allowance snapshots

| Field | Content |
|-------|---------|
| **Task ID** | INT-ST-01 |
| **Task name** | Periodic `account_snapshot_fact` / allowance |
| **Objective** | Record when `ConfiguredRiskPolicy` refreshes snapshots **and/or** timer every N s in reporting coordinator. |
| **Current** | Internal caches only. |
| **Change** | Hook inside `_capital_gate_eval` on refresh paths OR expose callback from risk to recorder. |
| **Affected** | `risk/configured.py`, optionally new `runtime/reporting_tick_actor.py` (if timer-based). |
| **Dependencies** | INT-RSK-01. |
| **Acceptance** | Live mode run shows ≥1 account snapshot when gate evaluated. |

#### INT-ST-02 — Position / exposure time series (read path)

| Field | Content |
|-------|---------|
| **Task ID** | INT-ST-02 |
| **Task name** | `position_fact` sampling |
| **Objective** | On fill events + interval: read `NautilusPositionStateReader` / `Portfolio.net_exposure` per instrument; store. |
| **Current** | No Tyrex callback on position change. |
| **Change** | From INT-ORD-01 fill handler trigger + optional heartbeat in reporting actor. |
| **Affected** | `reporting/position_sample.py`, `guru_compose` if actor registered. |
| **Dependencies** | INT-ORD-01, assembly exposes `position_state`. |
| **Acceptance** | After simulated fill in integration test, position fact non-empty. |

#### INT-RC-01 — Reconciliation stub

| Field | Content |
|-------|---------|
| **Task ID** | INT-RC-01 |
| **Task name** | `reconciliation_fact` v1 |
| **Objective** | Post-submit compare `Cache.order(coid)` vs expected qty/price; on mismatch emit fact (**framework path**). |
| **Current** | None. |
| **Change** | Call from `NautilusGuruExecutionPort` after `submit_order` (short delay optional) or on first order event. |
| **Affected** | `execution/nautilus_guru_exec.py` |
| **Dependencies** | INT-EXE-01. |
| **Acceptance** | Test with mock cache consistent/inconsistent. |

#### INT-BOOT-01 — Warmup / dynamic controller

| Field | Content |
|-------|---------|
| **Task ID** | INT-BOOT-01 |
| **Task name** | `component_status_fact` |
| **Objective** | Cache warmup success/failure; dynamic cap hits. |
| **Current** | Logs (`guru_cache_warmup.py`, dynamic resolve errors in exec). |
| **Affected** | `runtime/guru_cache_warmup.py`, `execution/nautilus_guru_exec.py` or `guru_instrument_dynamic.py` |
| **Dependencies** | INT-CMP-01. |
| **Acceptance** | Warmup fact links to run manifest trader_id. |

---

### B.4 Adapter / runtime dependency tasks

#### ADP-01 — Order-event completeness audit (Polymarket adapter)

| Field | Content |
|-------|---------|
| **Task ID** | ADP-01 |
| **Task name** | Verify fill/ack/reject events reach `Strategy.on_order_event` |
| **Gap** | If events missing, lifecycle facts lie. |
| **Blocks** | Execution truth, PnL. |
| **Owner** | Integration / adapter verification (Nautilus Polymarket exec client). |
| **Implementation** | Instrumented soak + document event types; open upstream issues if gaps. |
| **Reporting consumption** | Sets `data_quality.order_events_sparse`. |

#### ADP-02 — Tag propagation guarantee

| Field | Content |
|-------|---------|
| **Task ID** | ADP-02 |
| **Task name** | Confirm `Order.tags` visible on cached orders after submit |
| **Gap** | `is_guru_resting_order` falls back to COID—workable but obscures debugging. |
| **Implementation** | Adapter test: submit with tag → `cache.order` has tags. |
| **Reporting** | Emit `data_quality.tags_missing` sample rate. |

#### ADP-03 — Legacy py-clob execution parity

| Field | Content |
|-------|---------|
| **Task ID** | ADP-03 |
| **Task name** | Fill + status truth for HTTP path |
| **Gap** | No Tyrex subscription to user stream; `OrderEvent` may not track HTTP orders. |
| **Implementation options** | (a) Poll order status API in background reporter; (b) migrate live to framework submit; (c) document **non-canonical** execution truth for legacy with explicit flags. |
| **Reporting** | `summary.data_quality.legacy_execution_truth=partial`. |

#### ADP-04 — Mark price completeness for unrealized

| Field | Content |
|-------|---------|
| **Task ID** | ADP-04 |
| **Task name** | Marks available for non-flat instruments |
| **Gap** | Same as Phase B ops—unrealized PnL N/A when unresolved. |
| **Implementation** | Operational/data-client work; not reporting-only. |
| **Reporting** | `unrealized_pnl_unavailable_reason` populated from B1 `complete` flag patterns. |

#### ADP-05 — `venue_order_id` population timing

| Field | Content |
|-------|---------|
| **Task ID** | ADP-05 |
| **Task name** | Document latency ack → venue id |
| **Gap** | None if nullable join—**plan stays valid**. |
| **Implementation** | Confirm event order in logs; adjust ETL second-pass linking if needed. |
| **Reporting** | ETL links late venue id to lifecycle rows. |

---

### B.5 Report-generation tasks

#### RPT-01 — CLI `tyrex_report summarize`

| Field | Content |
|-------|---------|
| **Task ID** | RPT-01 |
| **Task name** | Build `summary.json` + `summary.md` from `facts.jsonl` or `run.sqlite` |
| **Inputs** | Run directory. |
| **Outputs** | Valid per SCH-04. |
| **Dependencies** | REC-05, SCH-04, SCH-03. |
| **Acceptance** | Exit non-zero on data_quality critical failures (optional flag). |

#### RPT-02 — Guru-vs-us section

| Field | Content |
|-------|---------|
| **Task ID** | RPT-02 |
| **Task name** | Join benchmark + sizing + risk + outcome + fills |
| **Objective** | Per `correlation_id` row: guru notionals vs ours; `delta_reason_code`; under/over classification. |
| **Dependencies** | RPT-01, INT-STR-01, INT-EXE-01, INT-ORD-01. |
| **Acceptance** | Golden run: known skip → correct taxonomy. |

#### RPT-03 — Execution quality metrics

| Field | Content |
|-------|---------|
| **Task ID** | RPT-03 |
| **Task name** | Slippage, time deltas, fill ratios |
| **Dependencies** | lifecycle + fill facts, SCH-03. |
| **Acceptance** | Median `time_to_first_fill_ms` computable or marked N/A with reason. |

#### RPT-04 — Capital deployment section

| Field | Content |
|-------|---------|
| **Task ID** | RPT-04 |
| **Task name** | Snapshots time series + gate impact |
| **Dependencies** | INT-ST-01, INT-RSK-02, INT-RSK-01. |
| **Acceptance** | Headroom to cap chart data in JSON (arrays). |

#### RPT-05 — Risk impact rollups

| Field | Content |
|-------|---------|
| **Task ID** | RPT-05 |
| **Task name** | Denies by gate; lost notional |
| **Dependencies** | INT-RSK-01, taxonomy. |
| **Acceptance** | Totals match manual sum of risk_decision_fact denies. |

#### RPT-06 — Token / market breakdown

| Field | Content |
|-------|---------|
| **Task ID** | RPT-06 |
| **Task name** | Top-K `token_id` by rejects, skips, volume |
| **Dependencies** | RPT-01. |
| **Acceptance** | Sort stable. |

#### RPT-07 — Anomaly + pipeline health section

| Field | Content |
|-------|---------|
| **Task ID** | RPT-07 |
| **Task name** | Ingest health + reporting health + reconciliation flags |
| **Dependencies** | INT-ING-02, REC-04. |
| **Acceptance** | Duplicate correlation_id detection surfaces in anomalies. |

#### RPT-08 — Config delta tool

| Field | Content |
|-------|---------|
| **Task ID** | RPT-08 |
| **Task name** | `tyrex_report diff-config run_a run_b` |
| **Objective** | Deep-diff `config_snapshot` JSON. |
| **Dependencies** | REC-06. |
| **Acceptance** | Shows changed YAML-effective fields only. |

#### RPT-09 — Cross-run index (optional catalog)

| Field | Content |
|-------|---------|
| **Task ID** | RPT-09 |
| **Task name** | Append run metadata to `var/reporting/index.sqlite` |
| **Dependencies** | REC-01. |
| **Acceptance** | List last N runs with mode + hash. |

---

### B.6 Validation tasks

#### VAL-01 — JSON Schema enforcement tests

| Field | Content |
|-------|---------|
| **Task ID** | VAL-01 |
| **Task name** | Every fact type golden dict validates |
| **Method** | `pytest` parametrized. |
| **Dependencies** | SCH-02. |
| **Acceptance** | 100% fact types covered. |

#### VAL-02 — Join integrity SQL

| Field | Content |
|-------|---------|
| **Task ID** | VAL-02 |
| **Task name** | `signal LEFT JOIN intent ON correlation` orphans report |
| **Method** | Queries in `tests/integration/test_reporting_joins.py`. |
| **Dependencies** | REC-05. |
| **Acceptance** | Orphans only for documented cases (e.g. crash mid-branch). |

#### VAL-03 — Cross-check Nautilus cache sample

| Field | Content |
|-------|---------|
| **Task ID** | VAL-03 |
| **Task name** | At end of integration test node, `cache.orders_open` consistent with last lifecycle |
| **Method** | Test with mocked or paper node if available; else mark heavy and run in CI nightly. |
| **Dependencies** | INT-ORD-01. |
| **Acceptance** | Document skip conditions. |

#### VAL-04 — Parity vs `guru_primary_report` / `guru_shadow_report`

| Field | Content |
|-------|---------|
| **Task ID** | VAL-04 |
| **Task name** | Same log fixture: script counts == summary counts |
| **Dependencies** | RPT-01, existing `scripts/guru_*_report.py`. |
| **Acceptance** | Automated for ingest health section. |

#### VAL-05 — Failure-mode tests

| Field | Content |
|-------|---------|
| **Task ID** | VAL-05 |
| **Task name** | Disk full, queue full, missing venue id, stale snapshot |
| **Method** | Mocks for sink; risk settings forcing stale allowance. |
| **Dependencies** | REC-03, SCH-05. |
| **Acceptance** | Correct `data_quality` flags; no uncaught exceptions in strategy. |

#### VAL-06 — Regression: unmapped ReasonCode

| Field | Content |
|-------|---------|
| **Task ID** | VAL-06 |
| **Task name** | Lint `ReasonCode` enum vs taxonomy map |
| **Dependencies** | SCH-03. |
| **Acceptance** | CI fails if new enum member not mapped. |

---

### B.7 Sequencing / dependency order

#### Prerequisite DAG (high level)

```
SCH-01 → SCH-02 → SCH-03, SCH-04, SCH-05, SCH-06
           ↓
REC-01 → REC-02 → REC-03 → REC-04, REC-07
           ↓          ↓
        REC-06     REC-05 → RPT-01 → RPT-02 … RPT-08
           ↓
INT-RUN-01 → INT-CMP-01 → [all INT-* modules in parallel clusters]
                              ↓
                         INT-ORD-01 depends on INT-EXE-01 (map fact)
                              ↓
                         INT-ST-02 depends on INT-ORD-01
RPT-* depend on REC-05 + integration complete enough for facts
VAL-* track parallel to RPT after REC-05
ADP-* can start immediately (audit); blocks labeled data_quality only
```

#### Critical path (longest technical dependency)

`SCH-01 → SCH-02 → REC-01 → REC-02 → REC-03 → INT-RUN-01 → INT-CMP-01 → INT-STR-01 / INT-RSK-01 / INT-EXE-01 → INT-ORD-01 → REC-05 → RPT-01 → RPT-02`

**Explanation:** Execution truth and guru-vs-us **require** lifecycle facts; lifecycle **requires** correlation map or proven COID inversion; strategy/risk/exe facts **require** recorder wiring from compose.

#### Parallelizable workstreams

| Stream | Tasks |
|--------|-------|
| **Schema/docs** | SCH-03, SCH-04, SCH-05, SCH-06 after SCH-02. |
| **Ingest** | INT-ING-01 … INT-ING-04 in parallel after INT-CMP-01. |
| **Reporting product** | RPT-05–RPT-09 in parallel after RPT-01 skeleton. |
| **Adapter audit** | ADP-01–ADP-05 independent until reporting consumes flags. |
| **Validation fixtures** | VAL-01 early; VAL-02+ after REC-05. |

#### Dependency chains touching adapter/runtime

- **INT-ORD-01** effectiveness → **ADP-01** (events exist).  
- **INT-ST-02 unrealized** → **ADP-04** (marks).  
- **Legacy live** → **ADP-03** before claiming full execution truth.

#### Phased execution order (implementation sequence only)

| Phase | Goal | Tasks |
|-------|------|-------|
| **P0** | Contract frozen | SCH-01–SCH-06 |
| **P1** | Durable facts on disk | REC-01–REC-04, REC-06–REC-07, INT-RUN-01, INT-CMP-01 |
| **P2** | Decision trace complete | INT-ING-01, INT-STR-01, INT-RSK-01, INT-EXE-01, INT-EXE-02 |
| **P3** | Execution truth | INT-ORD-01, INT-RC-01, ADP-01, ADP-02 |
| **P4** | Capital/position | INT-RSK-02, INT-ST-01, INT-ST-02, ADP-04 |
| **P5** | Ingest health + bootstrap | INT-ING-02–04, INT-BOOT-01 |
| **P6** | Analytical store + summaries | REC-05, RPT-01–RPT-08, RPT-09 optional |
| **P7** | Hardening | VAL-01–VAL-06, ADP-03, ADP-05 |

---

## Dependency-ordered task list (compact)

1. SCH-01, SCH-02, SCH-06  
2. REC-01, REC-02, REC-03, REC-04, REC-06, REC-07  
3. SCH-03, SCH-04, SCH-05  
4. INT-RUN-01, INT-CMP-01  
5. INT-ING-01, INT-STR-01, INT-RSK-01, INT-EXE-01, INT-EXE-02 (parallel)  
6. INT-EXE-01 before INT-ORD-01  
7. INT-ORD-01, INT-RC-01  
8. INT-RSK-02, INT-ST-01, INT-ST-02  
9. INT-ING-02, INT-ING-03, INT-ING-04, INT-BOOT-01  
10. REC-05  
11. RPT-01 → RPT-02–RPT-08 (+ RPT-09)  
12. VAL-01–VAL-06  
13. ADP-01–ADP-05 (start early; close ADP-03 before claiming legacy parity)

---

## Critical path summary

Freeze schemas → ship recorder + run wiring → emit **strategy/risk/execution** facts → emit **order_correlation_map + order events** → build SQLite → **guru-vs-us + execution quality** summaries → validation gates.

---

## Recommended implementation sequence (first 10 engineering steps)

1. **SCH-01 + SCH-02** — ids + Pydantic/TypedDict facts v1.  
2. **REC-01, REC-02, REC-03** — jsonl durability.  
3. **INT-RUN-01** — `run_id`, manifest, `config_snapshot`.  
4. **INT-CMP-01** — pass recorder into node builders.  
5. **INT-ING-01** — `guru_signal_fact` (validates end-to-end I/O).  
6. **INT-STR-01 + INT-RSK-01** — full pre-execution decision trace.  
7. **INT-EXE-01 + order_correlation_map** — submit + C3 structured.  
8. **INT-ORD-01** — lifecycle + fills.  
9. **REC-05 + RPT-01** — sqlite + first summary.json.  
10. **RPT-02 + VAL-02** — guru-vs-us + join tests.

**Information-value note:** Step 7–8 unlock the largest prior blind spot (venue feedback); prioritize immediately after decision trace (step 6), not “nice-to-have.”

---

## Implementation cut lines

### Required before claiming “reporting system is operational”

- **Durable run artifact**: `run_id`, `manifest`, `facts.jsonl` (or equivalent) written for every reporting-enabled run; **REC-03 + REC-07** behavior defined.  
- **Decision trace**: `guru_signal_fact` + `strategy_decision_fact` + `sizing_fact` + `risk_decision_fact` (allow+deny) + `execution_intent_fact` for guru-follow.  
- **Execution handoff**: `execution_outcome_fact` for framework + legacy submit/skip/error.  
- **Framework execution truth**: `order_lifecycle_fact` + `fill_fact` **for Nautilus `OrderEvent` streams that reach the strategy**, with **explicit `data_quality`** when event stream is empty.  
- **Post-run consumer**: `summary.json` + `summary.md` from facts (**RPT-01**) with **SCH-05** flags truthful.  
- **Join spine**: `correlation_id` ↔ `client_order_id` via **map fact** or deterministic reverse with tests (**SCH-01**).

### Can land later without losing foundational truth

- **RPT-08** config diff CLI, **RPT-09** global index.  
- **INT-RC-01** advanced reconciliation beyond first simple cache check.  
- **INT-ST-02** high-frequency position sampling (start with fill-triggered only).  
- **RPT-06** fancy token dashboards (basic counts suffice early).  
- **Parquet export**, BI connectors.

### Must not be mistaken for “complete”

- **Summaries from logs only** (regex scripts) as production reporting.  
- **Strategy accepts inferred only from `live_order_intent` logs** without `strategy_decision_fact` / `risk_decision_fact` allow rows.  
- **Legacy py-clob live** without **ADP-03** resolution—must show **`legacy_execution_truth=partial`** until fills are sourced.  
- **Empty `order_lifecycle_fact` table** on framework live runs **without** `data_quality.order_events_sparse` elevated to warning/fail—**that is a silent lie**.  
- **Unrealized PnL chart** when `ADP-04`/B1 marks incomplete—must display unavailable reason.

---

**Maintainers:** Keep this WBS synchronized with [`plan_reporting_observability.md`](plan_reporting_observability.md) when the architecture changes; bump task IDs or add amendment appendix rather than silent drift.
