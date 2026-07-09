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
| **Phase 1 survival (paired-binary)** | [Implementation/Survivor_target/phase1_parameter_guide.md](Implementation/Survivor_target/phase1_parameter_guide.md) · [modules/survival/README.md](modules/survival/README.md) |
| **Phase 2 WS backbone** | [Implementation/WebSocket_event_driven_backbone/phase_2.md](Implementation/WebSocket_event_driven_backbone/phase_2.md) · [modules/market_data/README.md](modules/market_data/README.md) |
| **Reading `facts.jsonl`** | [reporting_fact_model.md](reporting_fact_model.md) — fact catalog, joins, dedup. |
| **Normalizing recordings → Parquet** | [DATA_LAKE.md](DATA_LAKE.md) — M2B.3 offline data lake schema and CLI. |
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

[Implementation/](Implementation/) — rebuild plans, Phase 2 WS backbone, Phase 1 survival milestones.

**Current program status (2026-07):**

| Program | Status | Doc hub |
|---------|--------|---------|
| Phase 2 WS-primary backbone | **Complete** (M8 cutover, M9 baseline) | [WebSocket_event_driven_backbone/phase_2.md](Implementation/WebSocket_event_driven_backbone/phase_2.md) |
| Phase 4.6 paired binary | **Live-validated** | [architecture_enhance/phase_4_6_…](Implementation/architecture_enhance/phase_4_6_paired_binary_strategy_production_protection.md) |
| Phase 1 survival (damage control) | **Implemented**; enforce opt-in via scenarios | [Survivor_target/phase1_parameter_guide.md](Implementation/Survivor_target/phase1_parameter_guide.md) |
| P4 allocation ledger | Complete | — |
| P4 protection (TP/SL overlay) | Complete (separate from Phase 1 survival trailing) | [modules/protection/README.md](modules/protection/README.md) |
| P5 guru allocation-aware SELL | Code complete; live guru validation pending | — |
| Legacy sell_feature P6 TP/SL | Superseded by `protection/` overlay | [Implementation/sell_feature/README.md](Implementation/sell_feature/README.md) |

See [Implementation/sell_feature/README.md](Implementation/sell_feature/README.md) for older sell-phase history.
