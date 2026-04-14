# Tyrex_PM: Framework-First Lifecycle & Tradable-State Refactor (General Plan)

This document is the **authoritative general plan** for converging Tyrex_PM on a **NautilusTrader-first** architecture. **Child implementation plans** in this folder are **normative** for engineering: implementation should follow them without replanning unless code evidence invalidates a contract.

**Principle:** NautilusTrader + the Polymarket adapter own **trading-engine truth** (OMS, fills, positions, reconciliation **primitives**). Tyrex_PM owns **guru ingest**, **copy/Layer A policy**, **sizing**, **reporting**, **lifecycle orchestration** (startup/shutdown), and **risk/capital policy** that reads **declared** `TradableState` and `CapitalState` views—not parallel reconciliation or guessed deployment.

**Code evidence** cites Tyrex paths (`tyrex_pm/...`) and the **installed** `nautilus_trader` package (e.g. `adapters/polymarket/execution.py`, `live/execution_engine.py`, `system/kernel.py`). Pin versions in CI/docs when implementation starts.

**Child documents (normative contracts):** Each file below is authoritative for its topic. **Codability** (what can land in first PRs vs what needs a **spike exit** before production wiring) is summarized in **§9.1** — the architecture is settled, but phases are not uniformly “wire live on day one.”

| Document | Purpose |
|----------|---------|
| [`stabilization_roadmap.md`](stabilization_roadmap.md) | **Post-refactor** hardening, sequencing, and validation (from phase follow-ups) |
| [`tradable_state_health.md`](tradable_state_health.md) | Source of truth for OMS/trust health (**no fuzzy permanent design**) |
| [`collateral_unification.md`](collateral_unification.md) | Single **capital-state** contract and ownership |
| [`startup_readiness.md`](startup_readiness.md) | **Frozen** startup contract (ready / not-ready / modes); **T0** in §8.5.1 |
| [`shutdown_drain.md`](shutdown_drain.md) | **Mandatory** live cancel-and-drain vs framework stop/disconnect |
| [`execution_truth_alignment.md`](execution_truth_alignment.md) | Adapter config, position source, dynamic instruments |

**Frozen cross-references (no ambiguity):**

- **SELL under startup `DEGRADED`:** Same deny/allow rules as [`tradable_state_health.md`](tradable_state_health.md) §10 — startup mode only forbids **BUY**; **SELL** still requires trusted OMS/inventory (see [`startup_readiness.md`](startup_readiness.md) §8.1 / §8.4).
- **Startup readiness timeout `T0`:** Monotonic clock captured **immediately before** `TradingNode.run(...)` in `run_guru`, **after** successful `node.build()` — [`startup_readiness.md`](startup_readiness.md) §8.5.1.

---

## 1. Executive summary

**Clean target:** Tyrex is **policy + lifecycle orchestration** on top of Nautilus `Cache` / `Portfolio` / `LiveExecutionClient`. **Deployment** stays **derived** from open orders + open positions (`tyrex_pm/runtime/deployment_budget.py`)—no private counters. **Tradable trust** is **not** a pile of heuristics: it is either **framework-exposed status** or a **thin, typed bridge** to documented engine signals (see child plan). **Startup** follows a **single frozen contract** ([`startup_readiness.md`](startup_readiness.md)). **Live shutdown** follows **mandatory cancel-and-drain** unless an **explicit operator/dev override** is set ([`shutdown_drain.md`](shutdown_drain.md)). **Capital** follows one **CapitalState** contract ([`collateral_unification.md`](collateral_unification.md)).

**Problems today:** (1) Risk treats cache as implicitly safe while `LiveExecEngine` can log **persistent position discrepancy** (`live/execution_engine.py`) without Tyrex gating. (2) No startup readiness gate after `LiveExecEngineConfig` is wired (`tyrex_pm/runtime/guru_compose.py`). (3) `scripts/run_guru.py` does not cancel/drain; `PolymarketExecutionClient._disconnect` only closes websockets (`adapters/polymarket/execution.py`); `NautilusKernel.stop_async` disconnects engines/clients (`system/kernel.py`) **without** mass cancel—**venue orders can remain live**. (4) Collateral/allowance split: adapter `_update_account_state` vs Tyrex `ClobAllowanceStateProvider` (`tyrex_pm/runtime/state_readers.py`).

**Refactor goal:** **Module boundaries** and **frozen contracts** in child docs; **upstream Nautilus** where the framework should own signaling; **minimal Tyrex** only where the framework is genuinely silent—and then **documented as a bridge**, not a guess layer.

---

## 2. Framework-first capability map

Legend: **FW** = Nautilus core (live), **AD** = Polymarket adapter.

| Capability | Status | Where it lives | How Tyrex uses it |
|------------|--------|----------------|-------------------|
| Order submission | FW + AD | AD: `_submit_order`, `_post_signed_order` (`execution.py`). | Single path: `NautilusGuruExecutionPort` (`tyrex_pm/execution/nautilus_guru_exec.py`). |
| Own-order / trade (WS) | AD | USER WS, `_handle_ws_message` (`execution.py`). | No Tyrex WS parsing. |
| Open-order reconciliation | FW + AD | FW: `_check_orders_consistency` (`live/execution_engine.py`). AD: `generate_order_status_reports` / `get_orders` (`execution.py`). | Configure intervals via `guru_compose._live_exec_engine_config` → `LiveExecEngineConfig`. |
| Position reconciliation / repair | FW + AD | FW: `_process_cached_position_discrepancies`, `_query_and_find_missing_fills` (`execution_engine.py`). AD: `generate_position_status_reports`, `generate_fill_reports` (`execution.py`). | Tyrex does **not** repair; consumes **health** per [`tradable_state_health.md`](tradable_state_health.md). |
| Partial fills | FW + AD | AD: `generate_order_filled` (`execution.py`); leaves qty (`state_readers.OrderSnapshot`). | Deployment derivation only (`deployment_budget.py`). |
| Cancel / batch / cancel-all | AD | `_cancel_order`, `_batch_cancel_orders`, `_cancel_all_orders` (`execution.py`). | Shutdown issues **framework** commands ([`shutdown_drain.md`](shutdown_drain.md)). |
| Stop / disconnect | FW + AD | `NautilusKernel.stop_async` → `_disconnect_clients` (`kernel.py`). AD: `_disconnect` closes WS only (`execution.py`). | Tyrex **must** run drain **before** kernel stop for live. |

---

## 3. Current Tyrex module map

(Summary; unchanged structurally from prior revision.)

| Module | Aligns? | Main gap |
|--------|---------|----------|
| Guru ingest | Yes | — |
| Layer A / policy | Yes | No OMS logic here |
| Sizing | Yes | — |
| Risk | Partial | No health/capital contract |
| Deployment / readers | Partial | OK formula; split capital |
| Warmup | Partial | Not OMS readiness |
| Execution integration | Partial | No lifecycle |
| Shutdown | No | No drain |
| Reporting | Partial | Readiness/health/drain facts |

---

## 4. Target module architecture (summary)

- **Strategy / policy:** Guru, Layer A, sizing, intents only.
- **Runtime tradable state:** Façade over Nautilus reads + **typed** health (see child plan—not fuzzy).
- **Runtime capital state:** Single contract (see [`collateral_unification.md`](collateral_unification.md)).
- **Risk:** Reads `RiskStateView` = tradable + capital + health; behavior per status **frozen** in child docs.
- **Execution integration:** Submit path + lifecycle command issuance only.
- **Startup / shutdown:** Own modules/coordinators; contracts in child docs.
- **Reporting:** Readiness, health, capital freshness, drain outcome.

---

## 5. Interfaces (normative names)

| Interface | Owner | Consumers |
|-----------|-------|-----------|
| `TradableState` | Tyrex runtime (façade) | Risk, reporting |
| `TradableStateHealth` | **Framework signal or typed bridge**—see [`tradable_state_health.md`](tradable_state_health.md) | Risk, reporting |
| `CapitalState` | Tyrex runtime (single provider) | Risk, readiness, reporting |
| `RiskStateView` | Composed per evaluation | `ConfiguredRiskPolicy` |
| `StartupReadinessGate` | Tyrex lifecycle | `run_guru`, compose |
| `ShutdownDrainCoordinator` | Tyrex lifecycle | `run_guru` |
| `ExecutionLifecycleStatus` | Tyrex lifecycle + strategy | `CopyStrategy` |

---

## 6. Lifecycle phases

High level: **Boot → Connect → Readiness wait → Live → (Degraded) → Stop-requested → Cancel-and-drain → Disconnect → Terminated.**

Normative detail (owners, child-doc § refs): [`lifecycle_phase_contracts.md`](lifecycle_phase_contracts.md).

---

## 7. Ownership boundary table

| Concern | Clean owner | Tyrex keeps |
|---------|-------------|-------------|
| Order lifecycle, fills, positions | FW + AD | Submit + cancel **commands** at lifecycle boundaries |
| Reconciliation algorithms | FW | **Health consumption** via declared channel |
| Deployment formula | Tyrex on FW reads | `NautilusDeploymentBudget` |
| Capital balances / allowance | **One CapitalState** (adapter-fed + contract) | Normalized reads only |
| Startup readiness decision | Tyrex gate | Preconditions in child doc |
| Live shutdown cleanup | Tyrex orchestration | Mandatory default |
| Policy / Layer A / sizing | Tyrex | — |

---

## 8. Gap-to-target

Unchanged gist: aligned on submit + deployment derivation; needs health, capital, readiness, shutdown, alignment doc.

---

## 9. Phased program — **validated order**

**Dependency analysis (why reorder):**

| Phase | Depends on | Blocks |
|-------|------------|--------|
| Capital unification | — | Risk freshness, readiness precondition |
| Tradable state health | Capital optional for OMS-only health; **combined RiskStateView** needs capital | Startup readiness, risk behavior |
| Startup readiness | Health + capital contracts | Safe live entries |
| Shutdown drain | `ExecutionLifecycleStatus` (can land minimal flag with health phase) | Clean live exit |
| Execution truth alignment | Readiness/health operational | Stable HEALTHY signal |

**Rework risk if old order (health → startup → shutdown → collateral):** Risk and readiness would be recoded when capital unifies; **collateral first** minimizes churn.

### Recommended phases

1. **Phase 1 — CapitalState unification** ([`collateral_unification.md`](collateral_unification.md))  
2. **Phase 2 — TradableStateHealth** ([`tradable_state_health.md`](tradable_state_health.md))  
3. **Phase 3 — Startup readiness** ([`startup_readiness.md`](startup_readiness.md))  
4. **Phase 4 — Shutdown drain (mandatory live)** ([`shutdown_drain.md`](shutdown_drain.md))  
5. **Phase 5 — Execution truth alignment** ([`execution_truth_alignment.md`](execution_truth_alignment.md))  

**Exit criteria (program):** Each child doc §12–13; manifest/facts include readiness, health, capital freshness, drain outcome.

### 9.1 Implementation-ready vs spike-gated (by program phase)

Use this table to avoid treating “architecture-complete” as “production-wiring-complete.” **Immediately codable** means interfaces, frozen policy tables, and tests with mocks can ship without waiting. **Spike-gated** means a named question must be answered on the **pinned** Nautilus/Tyrex stack before live behavior matches the contract.

| Phase | Child doc | Immediately codable (early PRs) | Spike-gated for live wiring | Spike must answer | Spike exit criterion | Safe parallel work? |
|-------|-----------|----------------------------------|-----------------------------|-------------------|----------------------|----------------------|
| 1 | [`collateral_unification.md`](collateral_unification.md) | `CapitalState` DTO; single `CapitalStateProvider`; remove duplicate capital reads from risk; unit tests with mocked `Portfolio` | Capital **freshness** predicate vs real adapter account shape | After adapter refresh, does `Portfolio.account` expose **both** balance and allowance semantics the gate needs, or is **one** supplemental refresh required inside the provider? | Answer documented; implementation **only** inside `CapitalStateProvider`; contract tests updated | Phases 2–3 on **types and gates**; startup **§8.2 (capital fresh)** hardens after exit |
| 2 | [`tradable_state_health.md`](tradable_state_health.md) | Enum + DTOs; §10 matrix in `ConfiguredRiskPolicy`; facts; **unit tests with synthetic FW signals** | Live **`TradableStateHealthSnapshot` producer** | What bus topic, callback, or engine API proves reconciliation outcome on pinned Nautilus? | Path A vs B frozen (doc §5) + mapping table FW → enum; short spike note committed | Matrix + mocks in parallel; **production health transitions** block on exit |
| 3 | [`startup_readiness.md`](startup_readiness.md) | §8 evaluation order; status DTOs; reporting schema; gate **pure function** unit tests | Concurrent gate loop + §8.2.1 **exec connected** while `node.run` blocks | (a) Tyrex/Nautilus hook for timer/gate beside blocking `run`; (b) `exec_clients_ready(node) -> bool` on pinned stack | (a) Documented insertion point; (b) function + tests; see child §14.2 | Phase 1–2 policy in parallel; **live startup orchestration** blocks on (a)+(b) |
| 4 | [`shutdown_drain.md`](shutdown_drain.md) | Coordinator structure; polling; timeouts; facts; tests with mocked `Cache` | **CancelAll** command path | Which public `Strategy` / `Trader` API in pinned Nautilus issues strategy-scoped cancel-all routed to the adapter? | One call path documented + smoke verification | Skeleton PRs in parallel; **live drain** blocks on exit |
| 5 | [`execution_truth_alignment.md`](execution_truth_alignment.md) | YAML keys; loader tests; `InstrumentReadinessPolicy`; compose unit tests | `PolymarketExecClientConfig` / engine kwargs **into** the live node factory | Exact constructor/factory path from Tyrex `build_guru_trading_node` to adapter config today | Trace + kwargs surface or minimal factory patch; comments in code | After Phases 2–3 baselines; **prod alignment toggles** block on exit |

---

## 10. Pre-coding decisions (frozen in child docs)

Operational defaults are **fixed in child plans**. README-level **non-negotiables:**

1. **Health:** No permanent fuzzy heuristic architecture—see [`tradable_state_health.md`](tradable_state_health.md) for **framework vs bridge vs upstream** decision.
2. **Startup:** One contract—[`startup_readiness.md`](startup_readiness.md).
3. **Live shutdown:** Cancel-and-drain **mandatory**; override **explicit and non-default**—[`shutdown_drain.md`](shutdown_drain.md).
4. **Capital:** One ownership model—[`collateral_unification.md`](collateral_unification.md).
5. **SELL / degraded:** One matrix everywhere—[`tradable_state_health.md`](tradable_state_health.md) §10; startup [`startup_readiness.md`](startup_readiness.md) §8.1 / §8.4.
6. **Startup `T0`:** [`startup_readiness.md`](startup_readiness.md) §8.5.1 — not process start, not “when compose returns.”

---

## 11. Final recommendation

Implement in **Phase 1→5** order above. **Do not** add Tyrex reconciliation loops, deployment counters, or log-scraping as the **final** health design. **Do** contribute to Nautilus if reconciliation status is not observable on the message bus or public API.

**Post-refactor:** After the phased program is architecturally complete, engineering should execute **[`stabilization_roadmap.md`](stabilization_roadmap.md)**—hardening and live validation from the deferred items in `phase*_followup.md`, without reopening the framework-first design. **Execution detail:** merge **health producer wiring (WP2)** only after **initial** lifecycle/stop/drain hardening **(WP1)** lands; defer **capital reporting/schema churn (WP3)** until after the **first** live validation wave unless operators are blocked in practice (see roadmap §5–§7). **Validation scenarios:** `config/scenarios/stabilization_wave1/` (lifecycle/drain) and `config/scenarios/stabilization_wave2/` (health gate + WP2 producer); **WP2 `HEALTHY`** semantics are limited to the engine startup latch — see [`tradable_state_health.md`](tradable_state_health.md) §10.1.

---

## Package index

- [Post-refactor stabilization roadmap](stabilization_roadmap.md)  
- [Tradable state health](tradable_state_health.md)  
- [Collateral unification](collateral_unification.md)  
- [Phase 1 follow-up (deferred cleanup)](phase1_followup.md)  
- [Phase 2 follow-up (deferred cleanup)](phase2_followup.md)  
- [Phase 3 follow-up (deferred cleanup)](phase3_followup.md)  
- [Phase 4 follow-up (deferred cleanup)](phase4_followup.md)  
- [Startup readiness](startup_readiness.md)  
- [Shutdown drain](shutdown_drain.md)  
- [Execution truth alignment](execution_truth_alignment.md)  
- [Lifecycle phase contracts (cross-reference)](lifecycle_phase_contracts.md)
