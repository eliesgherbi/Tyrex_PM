# Tyrex_PM documentation index

**Start here** to find the right doc for your role. Paths are relative to the repo-root `Docs/` folder.

---

## By role

| I am… | Start with |
|--------|------------|
| **New to the repo** | [Architecture.md](Architecture.md) — what the system is and how modules connect. |
| **Operating a node** | [OPERATIONS.md](OPERATIONS.md) — modes, configs, logs, troubleshooting. |
| **Changing code** | [developer_guide.md](developer_guide.md) — ownership boundaries, where to add behavior, tests. |
| **Configuring YAML** | [CONFIG_MODEL.md](CONFIG_MODEL.md) — field tables for strategy / risk / runtime. |

---

## Current architecture & state

| Document | Purpose |
|----------|---------|
| [Architecture.md](Architecture.md) | End-to-end flow, module map, shadow vs live, framework vs legacy submit, **Phase A/B/C at a glance**. |
| [Implementation/current_state.md](Implementation/current_state.md) | **Maintainer hub:** what the codebase does today, live path matrix, failure classes, restart reality. |

---

## Operators (runbooks & validation)

| Document | Purpose |
|----------|---------|
| [OPERATIONS.md](OPERATIONS.md) | Main operator runbook: C1 ingest modes, Phase B matrix, logs, C2/C3 knobs, troubleshooting. |
| [CONFIG_MODEL.md](CONFIG_MODEL.md) | Authoritative YAML field reference. |
| [log_validation_playbook.md](log_validation_playbook.md) | Validation procedure; use with `OPERATIONS.md`. |
| [logging_system_guide.md](logging_system_guide.md) | Where logs go (`run_tyrex.log` vs `run_nautilus.log`). |
| [Runbooks/](Runbooks/) | Polymarket operator / order lifecycle notes (when linked from OPERATIONS). |

**Scripts (repo root):** `scripts/run_guru.py` (main entry), `scripts/guru_shadow_report.py`, `scripts/guru_primary_report.py`, `scripts/spike_rtds_activity.py`.

---

## Developers

| Document | Purpose |
|----------|---------|
| [developer_guide.md](developer_guide.md) | How to extend the system without breaking policy / risk / execution split. |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Short pointer to developer guide + quick setup/test commands. |
| [modules/README.md](modules/README.md) | Per-package README index under `src/tyrex_pm/`. |
| [dependency_lock.md](dependency_lock.md) | Dependency expectations. |

---

## Phase C & follow behavior (implementation depth)

**Planning / validation (canonical for design intent):**

| Doc | Notes |
|-----|--------|
| [Implementation/plan_C1_Time-to-Follow.md](Implementation/plan_C1_Time-to-Follow.md) | C1 design; **implemented** — RTDS + poll + gap-fill. |
| [Implementation/c1_shadow_run_guide.md](Implementation/c1_shadow_run_guide.md) | **Operator** C1 validation (shadow / primary / reports). |
| [Implementation/c1_closeout_note.md](Implementation/c1_closeout_note.md) | C1 closeout artifact. |
| [Implementation/plan_C2_Capital-Allocation.md](Implementation/plan_C2_Capital-Allocation.md) | C2 design; **implemented** (feature-flagged). |
| [Implementation/c2_validation_readiness_review.md](Implementation/c2_validation_readiness_review.md) | C2 validation / test inventory. |
| [Implementation/plan_C3_Execution-Quality.md](Implementation/plan_C3_Execution-Quality.md) | C3 design; **MVP implemented** on **framework-submit** path only (`NautilusGuruExecutionPort`). |

**Still primarily historical / program roadmap:**

| Doc | Notes |
|-----|--------|
| [Implementation/road_map.md](Implementation/road_map.md) | Strategic phases; cross-check **`current_state.md`** for what shipped. |
| [Implementation/Phase_B_planing.md](Implementation/Phase_B_planing.md) | Phase B normative design (B0–B5); **implemented**; §13 “Phase C” lists extra ideas beyond C1–C3 MVP. |
| [Implementation/phase_a_closure.md](Implementation/phase_a_closure.md) | Phase A / framework-truth checklist. |
| [Implementation/phase_b_operational_validation.md](Implementation/phase_b_operational_validation.md) | Pre–narrow-live B validation. |

**Migrations & audits (do not treat as current runbooks unless updated):**

- `documentation_reconciliation_2026-04.md`, `phase_c_merged_plan.md`, `phase_ab_test_validation_matrix.md`, logging review docs — **context**, not single source of truth for behavior.

---

## Maintenance

When you change **behavior** or **config loaders**, update in order:

1. Code + tests  
2. `CONFIG_MODEL.md` (if YAML surface changed)  
3. `OPERATIONS.md` (if operators need new grep lines or procedures)  
4. `Architecture.md` or `Implementation/current_state.md` (if the mental model shifts)  
5. Relevant `modules/*/README.md`

---

*This index is the navigation layer; detailed tables stay in CONFIG_MODEL and OPERATIONS.*
