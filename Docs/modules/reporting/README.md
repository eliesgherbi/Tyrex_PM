# Module: `tyrex_pm.reporting`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md) · [LIVE_ARCHITECTURE](../../LIVE_ARCHITECTURE.md) · [Fact model](../../reporting_fact_model.md) · [DEVELOPER.md](DEVELOPER.md)

## A. Role

**Structured observability for every run:** durable **`facts.jsonl`** (validated rows), optional **SQLite** ETL, **`summary.json` / `summary.md`** for operators and analysis. Records **strategy → risk → execution** and **Tier A/Tier B** observability (`venue_state`, `wallet_sync`, `risk_decision`, order lifecycle) without embedding trading policy inside the reporting package.

## B. Boundaries

**Belongs here:** `RunContext`, JSONL sink, fact envelope/schema validation, summarize, post-run DQ hooks, correlation registry helpers, **capital observability helpers** (`capital_observability.py`), order-event → fact mapping.

**Does not belong here:** `ConfiguredRiskPolicy` trading decisions; py-clob HTTP client; Nautilus strategy internals. **Capital normalization** (CLOB atomic strings → USD, Nautilus cash extract) lives in **`tyrex_pm.runtime`**; risk **emits** facts using those values.

## C. Internal structure

| Area | Contents |
|------|----------|
| `context.py` | `RunContext`, manifest paths, `emit` → sink. |
| `sinks/jsonl.py` | Batched writer, `fact_envelope` validation, pipeline health fact on close. |
| `schema/facts_v1.py` | Required keys per `fact_type`. |
| `schema/joins.md` | Join key contract. |
| `recorder.py` | `FactRecorder` / no-op. |
| `summarize.py` | `summary.json` v1: guru_vs_us, execution_quality, **capital_deployment**, risk_impact, etc. |
| `etl/jsonl_to_sqlite.py` | Post-run DB build. |
| `order_events.py` | Nautilus order events → lifecycle / fill facts. |
| `config_capture.py` | Frozen effective config snapshot (no secret values). |
| `capital_observability.py` | Summary/taxonomy helpers (venue denial heuristic, config parse); **not** CLOB wire format. |
| `__main__.py` | CLI: `build_db`, `summarize`. |

## D. Main interactions

- **`scripts/run_guru.py`** / **`runtime/guru_compose.py`:** when `runtime.reporting_enabled`, build `RunContext`, pass `emit` into strategy, risk, execution, ingest actors, **`WalletSyncActor` / `VenueState`** emit sites.
- **`risk/configured.py`:** emits `risk_decision`, `exposure`, `account_snapshot` (capital triggers).
- **`strategy/copy_strategy.py`:** strategy/sizing facts; order events; optional `emit_capital_observation` on submit / denial.
- **Operators:** `python -m tyrex_pm.reporting summarize --run-dir var/reporting/runs/<uuid>`.

## E. Status

**Operational** for manifest, config snapshot, facts spine, lifecycle/fill/position paths, **`venue_state`** / **`wallet_sync`** / **`deployment_budget`** rows, guru-vs-us summary, **capital facts** (canonical balance, CLOB normalization, venue denial flags). See [**reporting_fact_model.md**](../../reporting_fact_model.md). (**Removed:** `position_reconciliation` fact type — do not document.)

## F. Extension guidance

- New fact types: extend `facts_v1.py` required sets; add emit sites; extend summarize only when rollups need it.
- Prefer **emitting from the owning boundary** (risk, execution, strategy) with **thin** reporting imports.
- Keep **raw** venue/CLOB strings on facts; add **parallel normalized** fields rather than overwriting audit trail.
