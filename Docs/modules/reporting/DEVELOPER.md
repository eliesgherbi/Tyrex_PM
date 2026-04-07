# Developer guide — `tyrex_pm.reporting`

[README](README.md) · [reporting_fact_model.md](../../reporting_fact_model.md)

## Responsibility

**Durable run observability:** append-only **`facts.jsonl`**, **`manifest.json`**, optional SQLite ETL, post-run **`summarize`** rollups — without embedding trading rules inside the package.

## Architecture

- **`RunContext`** (`context.py`) — run id, paths, `emit` closure, manifest finalization (`run_ended_cleanly` from `run_guru.py`).
- **`sinks/jsonl.py`** — bounded queue, batch flush, schema envelope validation, pipeline health fact.
- **`schema/facts_v1.py`** — required keys per `fact_type` — **extend here first** when adding types.
- **`summarize.py`** — aggregates guru_vs_us, execution histograms, capital flags from JSONL (and optional DB).
- **`order_events.py`** — maps Nautilus order events → `order_lifecycle` / `fill`.

## Data flow

Emit sites (strategy, risk, execution, ingest) call `run_context.emit(dict)` with a **`fact_type`** and join keys (`run_id`, `correlation_id`, …). The sink validates, serializes JSONL, and updates queue stats.

## Extension workflow

1. Add/extend schema in `facts_v1.py` (required fields).
2. Emit from the **owning layer** (e.g. new execution lifecycle hook in `nautilus_guru_exec.py`), not from unrelated packages.
3. If summarize needs new rollups, extend `summarize.py` — keep JSONL the **source of truth**.

## Invariants

- **No secrets** in `config_snapshot` — `config_capture` masks material env.
- Prefer **raw + normalized** parallel fields on capital facts (do not overwrite audit strings).
- **`correlation_id`** should match guru pipeline id through risk and execution for joins.

## Pitfalls

- **Interrupt exit:** `run_ended_cleanly=false` in manifest does not necessarily mean invalid facts — often Ctrl+C.
- **Heavy emit paths:** use bounded queue; watch `reporting_sink_max_queue` in runtime YAML.

## Tests / CLI

`python -m tyrex_pm.reporting summarize --run-dir …` · ETL in `etl/jsonl_to_sqlite.py`.
