# Tyrex_PM documentation index

**Start here** by role. Paths are relative to the repo-root `Docs/` folder.

---

## By role

| I am… | Start with |
|--------|------------|
| **New to the repo** | [Architecture.md](Architecture.md) — what the system is and how modules connect. |
| **Operating a node** | [OPERATIONS.md](OPERATIONS.md) — modes, configs, logs, troubleshooting. |
| **Changing code** | [developer_guide.md](developer_guide.md) — ownership boundaries; then the relevant **`modules/*/DEVELOPER.md`**. |
| **Configuring YAML** | [CONFIG_MODEL.md](CONFIG_MODEL.md) — field tables for strategy / risk / runtime. |

---

## Documentation map (stable entry points)

| Document | Purpose |
|----------|---------|
| [Architecture.md](Architecture.md) | End-to-end flow, module map, shadow vs live, diagrams. |
| [developer_guide.md](developer_guide.md) | Where to add behavior, anti-patterns, test map. |
| [generale_workflow.md](generale_workflow.md) | Plain-language gate-by-gate walkthrough (guru → execution). |
| [CONFIG_MODEL.md](CONFIG_MODEL.md) | Authoritative YAML reference. |
| [OPERATIONS.md](OPERATIONS.md) | Operator runbook: ingest modes, deployment-budget risk, logs, reporting. |
| [reporting_fact_model.md](reporting_fact_model.md) | Structured reporting: join keys, fact semantics, CLI summarize. |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Quick setup + pointer to module guides. |
| [Implementation/current_state.md](Implementation/current_state.md) | Maintainer hub: what the code does today, failure classes. |
| [Implementation/end_to_end_review_logic.md](Implementation/end_to_end_review_logic.md) | Live path: signal → risk → execution → facts. |
| [Implementation/phase_b_operational_validation.md](Implementation/phase_b_operational_validation.md) | Live checklist for deployment-budget / restart behavior. |
| [Implementation/road_map.md](Implementation/road_map.md) | **Archived** phased plan — use Architecture + current_state for active behavior. |

---

## Module developer guides

Under [modules/](modules/README.md), mature packages include **`DEVELOPER.md`** (workflows, invariants, extension) beside **`README.md`** (short index).

---

## Runbooks

| Document | Purpose |
|----------|---------|
| [Runbooks/deployment_budget_live_validation.md](Runbooks/deployment_budget_live_validation.md) | CLI: live run + logs + summarize for deployment-budget risk. |
| [Runbooks/](Runbooks/) | Other operator notes (auth, order lifecycle, etc.). |

**Scripts (repo root):** `scripts/run_guru.py`, `scripts/guru_shadow_report.py`, `scripts/guru_primary_report.py`, `scripts/spike_rtds_activity.py`.

---

## Maintenance

When you change **behavior** or **config loaders**, update in order:

1. Code + tests  
2. `CONFIG_MODEL.md` (if YAML surface changed)  
3. `OPERATIONS.md` (if operators need new procedures or grep lines)  
4. `Architecture.md` or `Implementation/current_state.md` (if the mental model shifts)  
5. `reporting_fact_model.md` + `modules/reporting/*` (if fact types change)  
6. Relevant `modules/*/DEVELOPER.md`

---

*This index is navigation only; field tables live in CONFIG_MODEL and OPERATIONS.*
