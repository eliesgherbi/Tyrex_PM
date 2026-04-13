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

## Current status & operating model

**Framework truth (intentional):** Tyrex **deployment-budget** caps (`portfolio_deploy`, token deploy, pending rests) are computed from **Nautilus `Cache` + `Portfolio`** via `state_readers` / `NautilusDeploymentBudget`. **USDC balance / allowance** from py-clob is used only where **`capital_gate_enabled`** (and related fields) say so — it is **not** a substitute for “how much market risk is deployed” in the framework.

**What is in good shape for production-like guru follow today**

- **Startup instrument hydration** (wallet positions warmup + dynamic instruments) — hardened; see [Implementation/validate_startup_instrument_hydration.md](Implementation/validate_startup_instrument_hydration.md).
- **Runtime reconciliation prerequisites** (position/open-order check intervals wired from Tyrex) — see [Implementation/validate_runtime_reconciliation_prerequisites.md](Implementation/validate_runtime_reconciliation_prerequisites.md).
- **Scenario A (bot-originated sell)** — validation harness exercises **bot-owned** BUY → SELL on the same Nautilus order lifecycle; see [Implementation/validate_bot_originated_sell_scenario_a.md](Implementation/validate_bot_originated_sell_scenario_a.md).
- **Internal bot-owned lifecycle** is the **reliable** path for “did the framework free deployment after we closed exposure on the venue?”

**What is not fully solved**

- **External / manual / shared-wallet** activity (Polymarket UI sells, a second bot, another strategy on the same keys) can still **lag or diverge** from Nautilus `Cache` / `Portfolio` until reconciliation catches up — or indefinitely in edge cases. Tyrex does **not** promise instant multi-actor truth. See [Implementation/validate_manual_sell_reconciliation.md](Implementation/validate_manual_sell_reconciliation.md) and [Implementation/validate_runtime_reconciliation_behavior.md](Implementation/validate_runtime_reconciliation_behavior.md).
- **Recommended operating rule:** **one Tyrex bot instance ↔ one dedicated wallet**. Avoid routine manual trading on the **same** live wallet the bot uses.
- **Unresolved** prediction-market positions (still open on the venue) **continue to count** toward deployment / caps until the framework sees them closed (win or loss does not matter for “still deployed” until exit/reconcile).

**Where operators should read next:** [OPERATIONS.md](OPERATIONS.md) (full runbook + reconciliation mental model + limitations), [CONFIG_MODEL.md](CONFIG_MODEL.md) § Risk (deployment vs capital gate), [Implementation/review_nautilus_polymarket_reconciliation_model.md](Implementation/review_nautilus_polymarket_reconciliation_model.md) (deep reconciliation review).

---

## Validation & evidence (implementation docs)

Use these for **checklists, greps, and run artifacts** — not as a substitute for [OPERATIONS.md](OPERATIONS.md).

| Topic | Document |
|-------|----------|
| Startup wallet / instrument hydration | [Implementation/validate_startup_instrument_hydration.md](Implementation/validate_startup_instrument_hydration.md) · plan: [Implementation/plan_startup_instrument_hydration.md](Implementation/plan_startup_instrument_hydration.md) |
| Runtime reconciliation **wiring** (intervals, compose) | [Implementation/validate_runtime_reconciliation_prerequisites.md](Implementation/validate_runtime_reconciliation_prerequisites.md) · plan: [Implementation/plan_runtime_reconciliation_prerequisites.md](Implementation/plan_runtime_reconciliation_prerequisites.md) |
| Runtime reconciliation **behavior** (convergence, limits) | [Implementation/validate_runtime_reconciliation_behavior.md](Implementation/validate_runtime_reconciliation_behavior.md) |
| Scenario A — bot-originated sell | [Implementation/validate_bot_originated_sell_scenario_a.md](Implementation/validate_bot_originated_sell_scenario_a.md) |
| Manual / external sell | [Implementation/validate_manual_sell_reconciliation.md](Implementation/validate_manual_sell_reconciliation.md) |
| Nautilus + Polymarket reconciliation model | [Implementation/review_nautilus_polymarket_reconciliation_model.md](Implementation/review_nautilus_polymarket_reconciliation_model.md) |
| Deployment-budget truth (design background) | [Implementation/plan_deployment_budget_truth.md](Implementation/plan_deployment_budget_truth.md) |

---

## Documentation map (stable entry points)

**Folder naming:** Deep-dive implementation write-ups live under **`Docs/Implementation/`** (capital **I**). On Windows, `Docs/implementation/` may display as the same path; treat them as one directory and use **`Implementation`** in new links.

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
| [OPERATIONS.md](OPERATIONS.md) § *Current status & operating model* | Supported vs limited behavior; wallet model; links to validation docs. |
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
