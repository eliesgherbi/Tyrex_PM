# Tyrex_PM documentation

Polymarket-native trading stack. Documentation is grouped by audience.

## By role

| I am… | Start with |
|-------|------------|
| **New to the repo** | [Architecture.md](Architecture.md) — engines, module map, runtime diagram. |
| **Operating a node** | [OPERATIONS.md](OPERATIONS.md) — `tyrex-pm` CLI, env vars, run dirs, reporting. |
| **Changing code** | [developer_guide.md](developer_guide.md) · [modules/README.md](modules/README.md) — ownership, conventions, extension points. |
| **Setting up the dev env** | [DEVELOPMENT.md](DEVELOPMENT.md) — install, run, test, lint. |
| **Tuning configuration** | [CONFIG_MODEL.md](CONFIG_MODEL.md) — every YAML key + scenario layering. |
| **Reading `facts.jsonl`** | [reporting_fact_model.md](reporting_fact_model.md) — fact catalog, joins, dedup. |
| **Debugging a live race** | [LIVE_ARCHITECTURE.md](LIVE_ARCHITECTURE.md) — venue truth, reconcile state machine, in-flight reservations. |

## Top-level documents

| Document | Purpose |
|----------|---------|
| [Architecture.md](Architecture.md) | System overview, engines, runtime diagram, package map. |
| [LIVE_ARCHITECTURE.md](LIVE_ARCHITECTURE.md) | Venue vs local truth, reconcile / repair / adoption / tombstone state machines. |
| [OPERATIONS.md](OPERATIONS.md) | CLI, scenarios, env vars, reporting layout, on-call notes. |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Install, run, test, lint, CI expectations. |
| [developer_guide.md](developer_guide.md) | Code conventions, ownership boundaries, extension recipes. |
| [CONFIG_MODEL.md](CONFIG_MODEL.md) | YAML field reference (risk / runtime / strategy / scenario merge). |
| [reporting_fact_model.md](reporting_fact_model.md) | Fact-type catalog, payload shapes, join keys, dedup rules. |

## Per-module documentation

[modules/README.md](modules/README.md) — one short README per implemented package (`core`, `ingestion`, `signals`, `strategies`, `risk`, `execution`, `state`, `runtime`, `reporting`, `venue`).

## Historical / planning notes

[Implementation/](Implementation/) — original rebuild plan, event catalog, copy-strategy scope. Useful for archeology; the live source of truth is the code under `src/tyrex_pm/` and the documents above.
