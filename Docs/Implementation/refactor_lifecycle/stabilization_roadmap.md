# Post-refactor stabilization roadmap

## 1. Purpose

The **framework-first lifecycle refactor** (Phases 1–5) is **architecturally complete enough**: module boundaries, frozen contracts in child docs, and the NautilusTrader + Polymarket adapter as **trading-engine truth owners** are settled. Tyrex_PM remains **policy, reporting, and lifecycle orchestration** on top—not a parallel OMS or reconciliation layer.

This document governs the **post-refactor hardening and stabilization phase**. It turns the **accepted deferred items** in [`phase1_followup.md`](phase1_followup.md) through [`phase4_followup.md`](phase4_followup.md) into an actionable roadmap: workstreams, priorities, sequencing, dependencies, validation, and exit criteria. It does **not** reopen the refactor design or propose a second architecture pass.

---

## 2. Current state after the refactor

**Already achieved (summary):**

- **Single `CapitalState` path** and provider model; risk reads normalized capital views per [`collateral_unification.md`](collateral_unification.md).
- **`TradableStateHealth` contract** integrated into risk and reporting; policy matrices and facts exist per [`tradable_state_health.md`](tradable_state_health.md).
- **Startup readiness gate** with frozen evaluation order and reporting per [`startup_readiness.md`](startup_readiness.md).
- **Live shutdown cancel-and-drain** orchestration, facts, and `run_guru` integration per [`shutdown_drain.md`](shutdown_drain.md).
- **Execution truth alignment** knobs and readiness policy per [`execution_truth_alignment.md`](execution_truth_alignment.md).

**What the architecture guarantees:**

- Nautilus + adapter own order lifecycle, fills, positions, and reconciliation **primitives**; Tyrex issues submit/cancel **at declared lifecycle boundaries** and consumes **declared** health and capital views.
- Deployment stays **derived** from framework-backed reads (e.g. open orders / positions)—not private counters as the design authority.
- Child docs remain normative for behavior; stabilization **implements and hardens** them—it does not replace them.

**Intentionally deferred (tracked in phase follow-ups):**

- **Capital:** Narrow `CapitalStateProvider` coupling to full `RiskSettings`; clarify mixed-source capital **attribution** in reporting ([`phase1_followup.md`](phase1_followup.md)).
- **Reporting:** Synthetic health snapshot on “gate on, no producer” fail-closed path for operator joinability ([`phase2_followup.md`](phase2_followup.md)).
- **Lifecycle:** Worker-thread `node.stop()`, double-stop, startup vs drain ordering, reporting sink concurrency, compose injection discipline, cancel-loop error containment ([`phase3_followup.md`](phase3_followup.md), [`phase4_followup.md`](phase4_followup.md)).
- **Health producer:** README §9.1 Phase 2 “spike-gated” item—**production `TradableStateHealthSnapshot` producer** wired to **real** framework signals (Path A/B per [`tradable_state_health.md`](tradable_state_health.md))—remains the bridge from “types + mocks” to **live** health-driven readiness/risk transitions.

---

## 3. Stabilization principles

1. **Do not reopen the framework-first architecture**—no new truth layers, no redesign of Phase 1–5 scope.
2. **No Tyrex-side OMS truth patches**—no second reconciliation loop, no deployment counters or log-scraping as the **final** health design ([`README.md`](README.md) §10–11).
3. **No permanent health heuristics**—health stays framework-backed or a **thin, documented bridge**; placeholders and spike gates must **close** with a named producer strategy, not ossify.
4. **Hardening reduces operational risk without duplicating Nautilus**—stop ownership, drain error containment, and thread safety are **orchestration** concerns; they must not re-implement engine internals.
5. **Follow-up work stays within declared module boundaries**—capital cleanup stays inside provider/reporting contracts; lifecycle changes stay in coordinators, `run_guru`, and documented strategy/compose hooks.

---

## 4. Workstream map

### 4.1 Lifecycle hardening

**Objective:** Make startup failure paths, shutdown drain, and coordinator teardown **orderly, idempotent where required, and observable**—eliminating known race and error-propagation gaps without changing who owns trading truth.

**Source / origin:** [`phase3_followup.md`](phase3_followup.md) (A–D), [`phase4_followup.md`](phase4_followup.md) (B–D); partial overlap with [`phase4_followup.md`](phase4_followup.md) (A) as **monitoring** only unless Nautilus exposes a preferred API.

**Expected scope:**

- Worker-thread `node.stop()` and **double-stop** coordination ([`phase3_followup.md`](phase3_followup.md) A–B; [`phase4_followup.md`](phase4_followup.md) B).
- Startup terminal path vs `finally` drain ordering ([`phase4_followup.md`](phase4_followup.md) B).
- **Lifecycle state clobber risk:** startup coordinator still mutating status until `coord.stop()` while drain has begun ([`phase4_followup.md`](phase4_followup.md) C)—shutdown latch, join order, or single-writer rules.
- **Reporting sink concurrency** for facts/manifest from worker vs main ([`phase3_followup.md`](phase3_followup.md) C).
- **Uncaught cancel-loop exceptions** in drain—bounded handling, terminal `shutdown_drain` fact, best-effort `node.stop()` ([`phase4_followup.md`](phase4_followup.md) D).
- **Compose discipline:** assert or enforce risk + execution lifecycle injection together ([`phase3_followup.md`](phase3_followup.md) D).
- **Centralized stop ownership** as the clean target direction where it resolves A–B without fighting the framework.

**Why it matters:** These items drive **intermittent live failures**, confusing lifecycle facts, or **silent omission** of drain reporting on errors—directly undermining operator confidence.

**What it must not turn into:** A redesign of Nautilus shutdown semantics, a duplicate cancel engine, or Tyrex-owned reconciliation.

---

### 4.2 Health producer closure

**Objective:** Replace placeholder or spike-gated health sourcing with a **production `TradableStateHealthSnapshot` producer** aligned to [`tradable_state_health.md`](tradable_state_health.md) (framework signal vs thin bridge), preserving the **framework-first trust model**.

**Source / origin:** [`README.md`](README.md) §9.1 Phase 2 row (“spike-gated for live wiring”); operational dependency for **real** health transitions beyond unit/synthetic tests.

**Expected scope:**

- Execute or refresh the spike: pinned-stack **bus topic, callback, or engine API** → snapshot mapping table → typed enum/DTO emission.
- Wire producer into compose/runtime so `tradable_state_health_source` is non-`None` in production configs that enable the gate.
- Transition startup/readiness and risk from **fail-closed due to missing producer** (misconfiguration) to **health-driven** allow/deny per the frozen matrix—without new heuristics.

**Why it matters:** Until the producer is real, live runs either run **degraded** (gate off) or **fail closed** (gate on without source)—stabilization is incomplete for “trust the framework” operations.

**What it must not turn into:** Private counters, log scraping, or fuzzy permanent health rules that bypass Nautilus as truth owner.

---

### 4.3 Capital cleanup

**Objective:** Reduce coupling and **operator confusion** around capital snapshots while keeping the **single-provider model** intact.

**Source / origin:** [`phase1_followup.md`](phase1_followup.md) (A–B).

**Expected scope:**

- Narrow `CapitalStateProvider` API from full **`RiskSettings`** to a smaller **`CapitalSnapshotPolicy` / refresh policy** or an explicit `gate_requires_clob` supplied by `ConfiguredRiskPolicy` ([`phase1_followup.md`](phase1_followup.md) A).
- Clarify **mixed-source** reporting: when CLOB merge and Nautilus free collateral both contribute, facts should expose **per-field attribution** or a clearer contract than a single `source` ([`phase1_followup.md`](phase1_followup.md) B); update [`CONFIG_MODEL.md`](../../CONFIG_MODEL.md) / reporting schema as needed.

**Why it matters:** Wrong inference from `source` breaks capital debugging and downstream attribution; provider/risk coupling slows safe refactors.

**What it must not turn into:** Multiple competing capital read paths or Tyrex-computed “truth” balances that bypass the unified `CapitalState` contract.

---

### 4.4 Reporting / observability polish

**Objective:** Improve **operator clarity and joinability** of facts on edge paths without changing architectural ownership.

**Source / origin:** [`phase2_followup.md`](phase2_followup.md) (A).

**Expected scope:**

- On **health gate enabled + no source injected**: synthesize a **minimal** `TradableStateHealthSnapshot` for **emit/reporting only** (e.g. `UNKNOWN_BOOTSTRAP` + reason such as `health_source_missing`) while keeping the same risk deny outcome ([`phase2_followup.md`](phase2_followup.md)).
- Any **small** reason-code / fact-field polish explicitly accepted in phase follow-ups that improves dashboards without new architecture.

**Why it matters:** Deny paths should still produce **joinable** `tradable_state_health` rows for incident analysis.

**What it must not turn into:** Reporting that contradicts risk decisions, or new policy encoded only in logs/facts.

---

### 4.5 Live validation / stabilization

**Objective:** Prove the hardened stack under **realistic live and staging** conditions using **targeted scenarios**—startup, shutdown, partial fills, alignment config, and degraded/fail-closed behavior.

**Source / origin:** Cross-cutting proof of stabilization; aligns with program exit themes in [`README.md`](README.md) §9 and child doc §12–13 style criteria.

**Expected scope:**

- **Targeted validation runs** (curated scenario configs, not ad-hoc patching).
- **Startup path:** readiness, timeouts, terminal `NOT_READY` + exit behavior.
- **Shutdown / drain:** clean interrupt, drain timeouts, cancel partial failure paths after hardening.
- **Partial fills + residual orders:** deployment and drain behavior with open quantity edge cases.
- **Execution-alignment config:** `InstrumentReadinessPolicy`, adapter alignment knobs, facts visible in manifests.
- **Degraded / fail-closed:** capital stale, health unknown, missing producer—**deterministic** policy outcomes plus observable facts.

**Why it matters:** Stabilization is validated by **evidence**, not by doc completion alone.

**What it must not turn into:** A blanket “fix everything” pass or architecture redesign disguised as testing.

---

## 5. Priority and sequencing

**Priority ranking (urgency for live confidence):**

| Rank | Workstream | Rationale |
|------|------------|-----------|
| P0 | **4.1 Lifecycle hardening** | Directly affects **reliability** of stop/drain/facts on failure paths; unblocks trustworthy live exits and incident artifacts. |
| P0 | **4.2 Health producer closure** | Without a real producer, **gate-on live** cannot reflect true framework health; blocks “operate with trust” for health-gated configs. |
| P1 | **4.5 Live validation / stabilization** | Confirms P0 work; catches ordering/threading issues only live exercise exposes. |
| P2 | **4.3 Capital cleanup** | Reduces confusion and coupling; **does not** block engine truth; important for operator clarity and maintainability. |
| P3 | **4.4 Reporting / observability polish** | Improves misconfiguration visibility; safety already correct on deny path ([`phase2_followup.md`](phase2_followup.md)). |

**Dependency-aware sequencing:**

1. **Before broad live validation:** Close or explicitly time-box **lifecycle hardening** (4.1) items that affect `finally` drain, `node.stop()`, and cancel-loop containment—otherwise validation conflates **bugs** with **environment**.
2. **Controlled WP1 / WP2 parallelism:** **Spike and off-main-line exploration** for the health producer (4.2 / WP2) may run **in parallel** with lifecycle hardening (4.1 / WP1). **Do not merge** WP2 **production wiring** (compose/runtime integration, gate-on live paths) until **initial** WP1 hardening has **landed**—at minimum stop/drain/cancel-loop containment and a written stop-ownership decision—so health-producer debugging is not conflated with still-moving lifecycle and `node.stop()` behavior. Parallel work is fine; **integration timing** is sequential.
3. **Capital cleanup (4.3) without early reporting churn:** Narrowing **provider internals** (e.g. smaller policy inputs) may proceed when isolated. **Defer** mixed-source **reporting / schema / fact attribution** changes until **after** the **first** WP1 merge and **first** live validation **wave** (Wave 1 in §8), **unless** operators are blocked in practice or a production incident requires clearer capital fields sooner. Prefer additive, backward-compatible fact changes when attribution cannot wait.
4. **Reporting polish (4.4)** is a small, independent change set—schedule after or in parallel with 4.2 **if** touchpoints in risk/reporting are coordinated to avoid churn.
5. **Live validation (4.5)** runs in **waves**: smoke after 4.1 patches; full health-gated scenarios after 4.2 lands.

**What should happen before live validation:**

- Drain **error containment** and **terminal fact emission** (4.1 / Phase 4 follow-up D).
- Explicit decision on **stop ownership** or idempotent coordination for worker vs main (4.1 / Phase 3–4 B).
- **Health producer** either **landed** or **explicitly disabled** with documented operational mode (4.2)—validation scenarios must match the chosen mode.

**What can happen in parallel:**

- **WP2 spike** (signal mapping, tests on a branch) while WP1 is in flight—not **merged** WP2 integration until initial WP1 lands (see item 2 above).
- **WP3** provider-only refactors **without** fact/schema churn vs **WP4** (small polish), if touchpoints are coordinated.
- Unit/integration tests for 4.1 while the producer spike (4.2) is in flight.

**What should not be mixed:**

- **Architecture redesign** or new truth layers **with** stabilization PRs—separate PRs and reviews.
- **Large capital reporting/schema churn** **during** the first lifecycle hardening rollout and **first** live validation wave—stabilize lifecycle and get Wave 1 evidence first unless capital attribution **directly** blocks operator understanding or trading safety.

**What blocks live confidence vs not:**

- **Blocks:** Uncaught drain exceptions skipping facts/stop; undefined stop ordering; health gate on without producer when operators expect health-driven behavior.
- **Does not block:** Synthetic health row on misconfig path (4.4); cosmetic fact renames; optional attribution polish if operators can still use raw fields.

---

## 6. Work packages

### WP1 — Startup / shutdown lifecycle hardening

| Field | Content |
|-------|---------|
| **Scope** | Centralized or idempotent `node.stop()`; coordinator join / shutdown latch; worker vs `finally` drain ordering; reporting sink serialization or documented thread-safety; compose injection asserts ([`phase3_followup.md`](phase3_followup.md), [`phase4_followup.md`](phase4_followup.md)). |
| **Affected modules** | `scripts/run_guru.py` (or equivalent entry), `StartupReadinessCoordinator`, `ShutdownDrainCoordinator`, shutdown/startup helpers, reporting sink/manifest merge, `guru_compose` / strategy registration. |
| **Dependencies** | None architectural; may require Nautilus version notes for `TradingNode.stop()` behavior. |
| **Implementation shape** | Short design note in PR; bounded code changes with tests simulating timeout path, interrupt, and drain failure; optional feature flag only if required for rollback (prefer simplicity). |
| **Exit criteria** | No uncaught cancel exception aborting `finally` without terminal drain fact; startup worker cannot clobber drain phase after shutdown latch; documented single-owner stop policy; tests cover double-stop and worker/main interaction at unit/integration level. |

### WP2 — Health producer: spike closure + production wiring

| Field | Content |
|-------|---------|
| **Scope** | Pin stack signal source → `TradableStateHealthSnapshot`; compose/runtime wiring; config validation for gate+source pairing; documentation update in [`tradable_state_health.md`](tradable_state_health.md) if mapping table changes. |
| **Affected modules** | Runtime health producer package, `guru_compose`, loaders/config, risk/reporting consumers of snapshots. |
| **Dependencies** | Spike may start in parallel with WP1; **merge** production wiring **after** initial WP1 (stop/drain/cancel containment + stop policy) **lands** so integration tests and live debugging isolate producer behavior from moving lifecycle/stop semantics. |
| **Implementation shape** | Time-boxed spike doc + implementation PR; contract tests with recorded/synthetic FW signals; optional staging toggle. |
| **Exit criteria** | Live or staging run shows **non-placeholder** health transitions consistent with engine; readiness/risk use real snapshots under enabled gate; spike questions in README §9.1 Phase 2 marked **answered** in repo docs. |

### WP3 — Capital / runtime cleanup

| Field | Content |
|-------|---------|
| **Scope** | Narrow provider inputs; document and implement clearer mixed-source attribution in facts ([`phase1_followup.md`](phase1_followup.md)). |
| **Affected modules** | `CapitalStateProvider` implementation, `ConfiguredRiskPolicy` or adjacent policy types, reporting schema / facts, `CONFIG_MODEL.md` if fields change. |
| **Dependencies** | None on WP2. **Defer** reporting/schema/fact attribution slices until after initial WP1 + Wave 1 unless blocking in practice (see §5 item 3); coordinate with WP4 if the same reporting files change. |
| **Implementation shape** | Small DTO for refresh policy; backward-compatible fact fields where possible; migration notes for operators. |
| **Exit criteria** | Provider does not depend on full `RiskSettings`; operators can distinguish Nautilus vs CLOB contributions on mixed snapshots; tests for attribution and gate behavior unchanged logically. |

### WP4 — Reporting symmetry and clarity

| Field | Content |
|-------|---------|
| **Scope** | Emit minimal synthetic `tradable_state_health` on no-source fail-closed path ([`phase2_followup.md`](phase2_followup.md)); minor reason-code/fact polish as accepted. |
| **Affected modules** | Risk evaluation/reporting emit path, facts schema, tests. |
| **Dependencies** | Logical independence; schedule after WP2 if same files are hot. |
| **Implementation shape** | Small, isolated PR; snapshot synthesis **only** for reporting; no change to deny/allow outcome. |
| **Exit criteria** | Deny path produces joinable health fact with explicit reason; dashboards show one row per evaluation; unit tests lock behavior. |

### WP5 — Scenario-driven stabilization runs

| Field | Content |
|-------|---------|
| **Scope** | Curated scenario YAML + runbooks for startup, shutdown, partial fill/residual, alignment config, degraded modes; capture logs/manifests as evidence. |
| **Affected modules** | `config/scenarios/*`, ops docs as needed (minimal), no architecture changes. |
| **Dependencies** | WP1 for drain/stop reliability; WP2 for health-gated live proofs. |
| **Implementation shape** | Checklist-driven runs; **`config/scenarios/stabilization_wave5/RUNBOOK.md`** (commands, stop rules, evidence); pass/fail recorded in run notes or ticket. |
| **Exit criteria** | All P0 scenarios pass twice on target environment **or** failures map to filed bugs with severities; no known P0 lifecycle holes remain unowned. |

---

## 7. Risks and guardrails

| Risk | Guardrail |
|------|-----------|
| **Reopening architecture** | PR template / review checklist: “No new truth layer; links to child doc section if behavior changes.” |
| **Tyrex-side OMS truth patches** | Reject PRs that add reconciliation loops, deployment counters as authority, or health from logs only—route to Nautilus/adapter instead. |
| **Placeholders become permanent** | Time-box spike; **exit criterion** must name the producer owner and signal path; flag placeholder configs in ops docs. |
| **Validation mixed with redesign** | Label work **hardening** vs **polish**; freeze scope per WP; escalate scope creep to program owner. |
| **Reporting more confusing** | Prefer additive fields and docs; reason codes must match policy tables; user-test one dashboard join path before churning names. |
| **Threading regressions** | Load tests on readiness facts + manifest; stress interrupt during drain in staging only with safety limits. |
| **WP2 merged before WP1 stabilizes stop/drain** | Treat as execution error: keep spike on a branch; merge producer wiring only after initial WP1 lands. |
| **WP3 reporting churn during Wave 1** | Default **no**: finish provider-only work first or wait for Wave 1 unless attribution is operationally required. |

---

## 8. Validation strategy

**Unit / integration (mandatory for hardening):**

- Coordinator tests: timeout → stop path; interrupt during drain; cancel throws in loop → terminal fact still emitted.
- Compose tests: missing lifecycle or risk injection → fail fast.
- Reporting tests: synthetic health on misconfig path (WP4); capital attribution snapshots (WP3).

**Targeted live / staging:**

- **Wave 1 (post-WP1):** Shutdown drain with open orders; startup `NOT_READY` exit; KeyboardInterrupt during readiness wait. **Scenario bundle:** `config/scenarios/stabilization_wave1/` (health gate **off** to isolate lifecycle). **`HEALTHY` / health matrix is out of scope for Wave 1.**
- **Wave 2 (post-WP2):** Health transitions under load; gate enabled with **`NautilusLiveExecutionHealthSource`**; observe **`UNKNOWN_BOOTSTRAP` → `HEALTHY`** when the engine startup latch sets. **Scenario bundle:** `config/scenarios/stabilization_wave2/`. **Important:** WP2 **`HEALTHY` = startup reconciliation pass completed (latch)**, not full machine-readable OMS truth — see [`tradable_state_health.md`](tradable_state_health.md) §10.1. Degraded/divergent matrix spot checks remain **manual** until those levels are produced from framework signals.
- **Wave 3:** Capital-heavy scenarios; alignment toggles; partial-fill residual drill.

**Evidence before “done”:**

- Saved **manifest** excerpts or fact rows showing `startup_readiness`, `tradable_state_health`, `shutdown_drain`, `capital` freshness fields as applicable.
- Logs showing **ordering**: drain begin → cancels → terminal fact → stop (per implemented contract).
- For WP2: at least one run where health state **changes** in response to known engine condition (not static `UNKNOWN`).

**Confirmation metrics:**

- Absence of silent `finally` skips; drain `error_detail` populated when cancels fail; readiness facts present on all exit modes tested.

---

## 9. Definition of “stable enough”

**Stable enough to operate confidently** means:

1. **Startup is deterministic** for documented modes: ready, not-ready with configured exit, and timeouts behave per [`startup_readiness.md`](startup_readiness.md) without race-induced mystery states.
2. **Shutdown drain is reliable and observable:** cancel loop failures do not prevent terminal **drain** reporting and best-effort teardown; ordering relative to `node.stop()` is **defined** and tested.
3. **Health producer is real or explicitly resolved:** production configs either ship with a **wired producer** and validated mapping, or a **documented operational stance** (e.g. gate off) with no silent half-states.
4. **Capital semantics are not confusing:** mixed-source snapshots are **attributable**; provider inputs are **narrow**; operators know which field is authoritative for which concern.
5. **Live runs do not fail for known lifecycle reasons** captured in phase follow-ups; remaining issues are **operational** (venue, keys, liquidity) or **minor** polish, not structural threading or missing drain facts.
6. **Validation artifacts exist** for the P0 scenario set so on-call can trust playbooks.

---

## 10. Final recommendation

**Immediate next step:** Execute **WP1 (lifecycle hardening)** starting with **cancel-loop containment + terminal drain fact** and a **written stop-ownership decision** (single gate vs idempotent contract), then add tests for worker/main interaction. In **parallel**, time-box the **health producer spike** (WP2) on a branch if staffing allows—**merge WP2 production wiring only after initial WP1 has landed**, so health integration is not debugged against unstable stop/drain behavior.

**Do first:** Drain error handling and stop/drain ordering—these block trustworthy live validation.

**Do not do now:** Second OMS layer, heuristic health final design, broad refactors unrelated to follow-ups, or splitting stabilization across many new planning docs.

**Planning docs:** **No further planning pass is required** for engineering to start execution—the phase follow-ups plus this roadmap are sufficient. Add **short spike notes or runbook updates** only as **outputs of work**, not as a prerequisite to beginning WP1/WP2.

---

## References

- [`README.md`](README.md) — program framing and §9.1 spike table  
- [`phase1_followup.md`](phase1_followup.md) — capital deferred items  
- [`phase2_followup.md`](phase2_followup.md) — reporting symmetry  
- [`phase3_followup.md`](phase3_followup.md) — startup lifecycle deferred items  
- [`phase4_followup.md`](phase4_followup.md) — shutdown drain deferred items  
- Child contracts: [`tradable_state_health.md`](tradable_state_health.md), [`collateral_unification.md`](collateral_unification.md), [`startup_readiness.md`](startup_readiness.md), [`shutdown_drain.md`](shutdown_drain.md), [`execution_truth_alignment.md`](execution_truth_alignment.md)
