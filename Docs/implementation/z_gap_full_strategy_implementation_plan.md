# Z-Gap full strategy — implementation plan

**Status:** P0 frozen — implementation-ready design plan (**no F1 code**)  
**Date:** 2026-07-19 (architecture review; P0 freeze 2026-07-20)  
**Architecture review:** **PASS WITH CORRECTIONS** (thin strategy / reusable mechanism; F1 generic actions; atomic epochs)  
**Strategy baseline:** [`z0_z_gap_full_strategy_spectrum.md`](z0_z_gap_full_strategy_spectrum.md) §0 (corrections A–F)  
**Legacy Phase A evidence (not the identity):** [`z0_z_gap_design_audit.md`](z0_z_gap_design_audit.md)  
**Framework baseline:** `fb9d0d8` · docs/test `0a48dfc` · branch `rest_project`

This plan is derived from the **accepted full-strategy identity**, not from legacy Phase A/B/C roadmaps.

**Central separation (non-negotiable):**

```text
ZGapStrategy = thin hypothesis and decision coordinator
≠ data adapter + calculator + risk engine + order manager + portfolio

Tyrex_PM framework = reusable mechanism
≠ a hidden Z-Gap strategy implementation

Reusable mechanism ≠ Z-Gap-specific policy
```

---

## A. Executive implementation verdict

**Yes — the full strategy can be implemented incrementally on R8**, if Z-Gap remains a thin orchestrator over pure Z-Gap policy modules, while framework hosts own data truth, risk, planning, OMS, portfolio, lifecycle, persistence, and reporting.

| Category | Assessment |
|----------|------------|
| **Main architectural gaps** | Neutral `StrategyDecision` + `IntentLike`; immutable Z-Gap decision snapshot; framework-owned stores/indicators feeding that snapshot; Z-Gap pure valuation + policy modules; host wiring without Z-Gap formulas; timer cadence; resolution capability |
| **Reuse directly** | Dispatcher; book/reference stores; intents; RiskEngine; planners; ShadowOMS; OrderStore/FillLedger/Portfolio; TradeLifecycle; persistence; `fees_fd` helpers; fact sink; discovery helpers |
| **Needs extension** | Protocol/decisions/context; generic economics *value types* only; DecisionContext/lifecycle snapshots; reporting taxonomy; generic host evaluate path |
| **Quantitatively reusable (new)** | EWMA volatility; digital \(\Phi(z)\) fair value; all-in entry cost / net-sell unit math (fee-sign correct) |
| **Z-Gap-specific (keep out of core)** | Leg selection, readiness gates, entry/exit policies, thesis confirm, sell/continue/resolve preference, reason taxonomy, config |
| **Future only** | Continuous live; real redeem; R7 ack/residual CLI |
| **Genuine blockers before F1** | None if this architecture review is accepted. PTB provider choice stays open through F1 (fixtures). |

**Hard rules:** no `old/` imports; no Z-Gap → adapters / `execution.polymarket` / `runtime/r7*` / RiskEngine / planners / OMS / portfolio stores / persistence / reporting writers; no Z-Gap formulas in generic hosts; no `isinstance(strategy, ZGapStrategy)` branching; OBSERVE and SHADOW call the **same** pure decision path.

---

## A2. Architecture review — responsibility decomposition

### A2.1 Thin `ZGapStrategy` contract

**May:**

- receive normalized `ZGapSignal` / `ZGapDecisionSnapshot` + framework `DecisionContext`;
- hold only minimal strategy-private decision state (§A2.4);
- call pure Z-Gap valuation and policy functions;
- map economic actions → `IntentLike`;
- return `StrategyDecision` with reasons/evidence.

**Must not:**

- call Polymarket/Binance/Chainlink/Gamma or any adapter;
- reconstruct books; sync clocks; capture/lock PTB;
- process raw ticks into σ inside the strategy class;
- query `Portfolio` / `OrderStore` / `FillLedger`;
- determine confirmed/sellable quantity;
- compute venue limit prices, tick rounding, FAK/GTC;
- call `RiskEngine`; submit/cancel orders;
- process raw OMS/venue events; reconcile; retry;
- persist operational state; write JSONL facts;
- import `execution.polymarket`, `runtime.r7*`, or adapters.

**Says:** “Enter DOWN — round-trip-aware edge valid.” / “Become flat — market richness exhausted / thesis invalid.”  
**Never says:** “FAK SELL 7.42 @ 0.61.”

### A2.2 Opposite mistake (forbidden)

Framework/generic hosts must **not** decide: underpriced leg, model validity, rich exit, thesis invalidation, sell-vs-resolution preference, Z-Gap thresholds/confirmation. Those stay in **Z-Gap-owned pure policy modules**.

### A2.3 Reuse-level classification

| Component | Level | Rationale / reusability test |
|-----------|-------|------------------------------|
| `StrategyDecision`, `IntentLike`, protocol | **A Framework-generic** | Any strategy; no Z-Gap thresholds |
| `DecisionContext`, lifecycle/position **snapshots** | **A** | Immutable views; not stores |
| Clock / time-authority contracts | **A** | Any strategy |
| Executable quote/depth representations | **A** or market_data | Mechanism |
| Generic fee truth names / all-in cost & proceeds **value types** | **A** (`core/economics` only if truly generic) | Stable semantics; no Z-Gap config |
| Order/fill/position/lifecycle/risk/plan/OMS/persistence/facts | **A** | Already R8 |
| Resolution rule, frozen reference-level (K) snapshot, binary payout EV helpers | **B Binary-market reusable** | Place under `domain/polymarket` (or binary domain); **not** forced into `core` |
| EWMA volatility | **C Quantitatively reusable** | `indicators/`; no Z-Gap config inside estimator |
| Digital fair value \(\Phi(z)\) | **C** | Pure; binary digital math; no Z-Gap gates |
| Net sell unit / entry all-in unit math | **C** | Fee-sign correct pure functions; thresholds stay outside |
| Z-Gap readiness, leg select, entry/position **policy**, thesis, no-reentry, sell/continue/resolve, reasons, config | **D Z-Gap-specific** | `strategies/z_gap/` |
| Z-Gap model **composition** (which reusable pieces + gates) | **D** | May call C/B; owns wiring of Z-Gap hypothesis |
| Binance/PTB/Gamma adapters | **E Integration** | Emit normalized contracts only |
| Observe/Shadow hosts, CLI, fact sink wiring | **F Composition** | May import concretes; **no Z-Gap formulas** |

**Reusability test applied:** if a name embeds Z-Gap thresholds/reasons or has no second consumer, it stays **D**. Speculative “maybe later” is not enough to put policy in `core`.

### A2.4 Cohesive module grouping (smallest set)

Prefer a few cohesive pure modules over a giant `strategy.py` or a one-function-class forest.

**Conceptual layout (not mandatory filenames):**

```text
Framework-generic
  strategies/{protocol,decisions,context}.py
  core/{intents.py, economics.py}          # value types only
  indicators/ewma_volatility.py            # C
  indicators/binary_fair_value.py          # C (digital Φ)
  market_data/ executable quotes           # A
  domain/polymarket/ resolution + K types  # B
  risk / planning / execution / portfolio / lifecycle / reporting / persistence

Z-Gap feature (D)
  strategies/z_gap/
    strategy.py          # thin bridge only
    config.py
    valuations.py        # entry + position valuation (pure; may call C)
    policies.py          # readiness, entry, thesis, realization, precedence
    state.py             # minimal private decision state
    reasons.py
  signals/z_gap.py       # optional snapshot builder (pure or host-called)

Integration (E) + Composition (F)
  adapters/* ; runtime hosts ; application/cli
```

**Valuation placement (chosen):** valuation and policy are **pure functions inside `strategies/z_gap/`** (and C helpers they call), invoked by the thin strategy (or by a signal builder that is still Z-Gap-owned and host-invoked).  
**Not** embedded in generic host callbacks.  
**Not** duplicated between OBSERVE and SHADOW.

### A2.5 Required decision flow

```text
External payload
→ adapter (E)
→ normalized event
→ authoritative store (A)
→ reusable indicators (C)
→ normalized ZGapDecisionSnapshot / ZGapSignal (D builder or host assembler)
→ Z-Gap valuation (D, pure)
→ Z-Gap decision policy (D, pure)
→ thin ZGapStrategy (D orchestrator)
→ StrategyDecision + economic intents (A contracts)
→ shared RiskEngine (A)
→ shared Planner (A)
→ OMS (A)
→ execution events
→ OrderStore / FillLedger / Portfolio / Lifecycle (A)
→ next DecisionContext snapshots (A)
→ reporting sink records outputs (A)  — no recalculation of FV/PnL/richness
```

OBSERVE vs SHADOW: **identical** Z-Gap decision functions; only OMS dispatch differs.

**Unacceptable:** formulas in strategy callbacks; Z-Gap math in generic host; `isinstance(ZGapStrategy)`; duplicated observe/shadow decision engines.

### A2.6 `ZGapStrategy` responsibility budget

```text
on_start(context)
  → init minimal private decision state

on_signal(z_gap_signal, context)
  → branch on lifecycle *view* from context (not stores)
  → delegate valuation + policy (pure)
  → map action → IntentLike (economic fields only)
  → return StrategyDecision + intents

on_stop(reason)
  → clear private decision state only
```

| Strategy-private field | Why not derived from framework |
|------------------------|--------------------------------|
| Decision epoch / window id binding | Strategy’s “idea generation” counter beyond lifecycle |
| `entry_lineage_attempted` / `window_closed_to_reentry` | Hypothesis policy; may outlive a failed unfilled attempt per config |
| Thesis confirmation progress | Z-Gap policy state (or owned by pure stateful policy object passed through) |
| Irreversible resolution commitment flag | Economic point-of-no-return preference once set (until framework marks resolution-pending) |

**Must not duplicate:** order pending, filled qty, exit pending, confirmed flatness, residual qty, unknown submission — those are framework lifecycle/inventory.

### A2.7 State ownership matrix

| State | Authoritative owner | Consumer | Persistence owner | Must not duplicate in |
|-------|---------------------|----------|-------------------|------------------------|
| Market/window identity | Instrument/registry + host config | Strategy (snapshot) | Host/config | strategy private stores |
| PTB/K lock + provenance | PTB store/service | Snapshot → strategy | PTB store | `ZGapStrategy` |
| Binance/reference \(S\) | ReferenceDataStore | Indicators + snapshot | Optional | strategy |
| Volatility estimator σ | Indicator instance (host-held) | Snapshot builder | Optional snapshot of σ | strategy class tick loop |
| Model snapshot \(z,p,\tau\) | Built from C+inputs (ephemeral) | Valuation/policy | Facts only | host formulas |
| Entry decision epoch / no-reentry | **Z-Gap strategy/policy** | Policy | Strategy slice in host snapshot if needed | TradeLifecycle |
| Orders | OrderStore | Planner/OMS/lifecycle | StateSnapshotStore | strategy |
| Fills | FillLedger | Portfolio | StateSnapshotStore | strategy |
| Position qty + cost basis | Portfolio | Context snapshot → valuation | StateSnapshotStore | strategy |
| Lifecycle phase / exit pending | TradeLifecycle | Context → strategy branch | StateSnapshotStore | strategy |
| Thesis confirmation | Z-Gap policy state | Policy | Strategy slice | TradeLifecycle |
| Fee estimates | Valuation inputs / fee resolver | Valuation | Facts | reporting recalculation |
| Confirmed actual fees | Fill/venue evidence | Portfolio/reporting | FillLedger / reports | strategy inventing fees |
| Resolution commitment | Z-Gap policy until PONR; then lifecycle | Policy + lifecycle | Both as appropriate | two authorities after PONR |
| Resolution result | Resolution evidence service | Portfolio/lifecycle | That service | strategy |
| Inventory classification | Settlement/recon (venue) + portfolio internal | Host | Recon artifacts | strategy |
| Terminal outcome/provenance | Lifecycle / host terminal | Reporting | Host | inventory enum |

### A2.8 Immutable decision snapshot (atomic epoch)

See **§D.2**. Strategy evaluation uses one sealed epoch (`ZGapDecisionSnapshot` **or** `ZGapSignal` + `DecisionContext` sharing one snapshot/epoch identity). No cross-time mashups; no mutable store fetches.

**Dependency note (active debt):** `DecisionContext` today imports `LifecycleSnapshot` from `lifecycle` — acceptable for **immutable snapshot types**. Strategy must still not import portfolio/order **stores**. F1 keeps snapshot-only imports; if needed, move snapshot DTOs to `core`/`strategies`.

### A2.9 Valuation-module ownership

| Module | Owns formulas | Must not |
|--------|---------------|----------|
| Entry valuation (pure, Z-Gap + C helpers) | \(C_{\mathrm{entry,unit}}\), \(E_{\mathrm{settlement}}\), \(E_{\mathrm{repricing}}\), max acceptable **economic** price evidence | Order qty, tick round, FAK |
| Position valuation (pure) | \(V_{\mathrm{sell}}\), richness, remaining edge, resolve values, liquidation PnL | Confirmed qty invention; venue SELL cmd |
| Policy (pure) | Gates, leg select, thesis, realization, precedence | Recompute valuation formulas |
| Reporting | Records valuation **outputs** from decisions/facts | Recalculate FV/PnL/richness |

### A2.10 Planning / execution / risk / reporting separation

| Layer | Receives | Owns |
|-------|----------|------|
| Strategy | Economic desire + evidence (e.g. max acceptable entry price as **evidence**) | Intent reason, notional request, urgency/reason |
| Risk | Intent + RiskContext | Approve/deny shared caps, kill, unknown, liquidity floors |
| Planner | Approved intent | Qty from portfolio/sellable rules, side, tick price, style, deadline |
| OMS | Plan/commands | Submit/cancel |
| Lifecycle | Execution events | Outstanding work, dup suppression |
| Reporting | Stream of envelopes | Append-only facts; **no** recompute |

Intent/risk bridge fields (minimal): `target_notional`, optional `max_price` evidence, `reason_code`, `semantic_key`, correlation ids, mode — **not** Z-Gap φ formulas inside RiskEngine.

### A2.11 Static dependency / allowed-import matrix

| Package | Must not import |
|---------|-----------------|
| `core` | strategies, adapters, runtime, execution |
| `indicators` | adapters, OMS, portfolio, `runtime.r7*` |
| `signals` | concrete execution, adapters |
| `strategies/z_gap` | adapters, risk engine, planners, OMS, portfolio **stores**, persistence, reporting **writers**, `execution.polymarket`, `runtime.r7*` |
| `risk` / `planning` / `execution` | concrete strategies (`strategies.z_gap`) |
| `adapters` | may depend inward on domain/core contracts |
| `runtime` / `application` | may wire concretes (**F**); must not embed Z-Gap valuation formulas |

### A2.12 Architecture enforcement tests (planned)

- Protocol does not import `framework_validation`
- F1 `StrategyDecision.action` vocabulary has no `HOLD_TO_RESOLUTION` (resolution is F5 intent)
- `strategies/z_gap` has no adapter / `execution.polymarket` / OMS / risk / planning / portfolio-store / persistence / reporting-writer / `runtime.r7*` imports
- `risk` / `planning` / `execution` have no `strategies.z_gap` imports
- No `old/` imports anywhere in `src`
- Generic observe/shadow host modules contain no Z-Gap φ/`E_repricing`/richness formulas
- Same pure decision entrypoint referenced by observe and shadow wiring tests
- Valuation fee-sign / richness formulas defined once
- Reporting consumes recorded valuation fields; does not call fair-value helpers
- Mismatched snapshot epochs rejected; stale components cannot silently decide
- OBSERVE and SHADOW build equivalent sealed decision inputs
- Strategy evaluation does not access mutable stores
- ReferenceMomentum still passes via neutral protocol
- F1 action set excludes resolution-hold; F5 requires explicit resolution request for lifecycle transition

Prefer import/architecture + behavioral contracts over brittle exact path lists.

---

## B. Current-to-target architecture mapping

| Functional need | Existing R8 component | Gap | Target owner | Change type |
|-----------------|----------------------|-----|--------------|-------------|
| Market discovery | `adapters/polymarket/discovery`, CLI `discover-btc-window`, `btc_5m_window` | Resolution-rule packaging | adapters + domain | extend |
| Resolution rule | Partial market metadata | Explicit rule object (UP iff …) | domain/polymarket | new shared contract |
| PTB/K | **missing** | K snapshot + quality/lock | integration port + state | new shared contract + new strategy-specific consumer |
| Time authority | `Clock` / `FakeClock` | Corrected now + uncertainty_ms | core/runtime clock | new shared contract |
| Binance/reference alignment | Binance adapter + `ReferenceDataStore` | Settlement-associated ref + basis | adapters + market_data | extend |
| Volatility | — | EWMA + jump | `indicators` (**C**) | new quantitatively reusable |
| Fair value \(\Phi(z)\) | — | Pure digital FV | `indicators` (**C**) | new quantitatively reusable |
| Fees and friction | `fees_fd.py`, shadow fee config | φ helpers + generic cost/proceeds types (**A/C**); Z-Gap thresholds stay **D** | execution + core economics types | extend |
| Entry opportunity | ObserveHost + momentum path | Z-Gap valuation+policy (**D**); thin strategy | `strategies/z_gap` | new Z-Gap-specific |
| Active position valuation | Partial via DecisionContext | Position valuation pure (**D** + **C** helpers) | `strategies/z_gap` | new Z-Gap-specific |
| Strategy decisions | `ObserveDecision` leak | Neutral decision + actions (**A**) | `strategies/decisions` | new shared contract |
| Intents | core intents | Protocol widen to IntentLike | strategies/protocol | extend |
| Risk | `RiskEngine` | Shared caps/kill/unknown; not all model gates | risk | reuse + extend policies lightly |
| Entry planning | `ExecutionPlanner` | Model max price evidence | planning | reuse / extend |
| Exit planning | `ExitPlanner`; R7 FAK planner | Shadow: ExitPlanner; live later | planning | reuse; promote later |
| OMS | ShadowOMS / LiveOMS | Shadow for F4; live future | execution | reuse unchanged (shadow) |
| Orders/fills | OrderStore, FillLedger | — | execution | reuse unchanged |
| Portfolio | Portfolio | Cost basis fields for Z-Gap facts | portfolio | extend lightly |
| Lifecycle | TradeLifecycle | Resolution-pending phase/flag | lifecycle | extend |
| Resolution | settlement flatness only | Outcome/payout simulation (F5); live redeem future | domain + runtime | new / future only |
| Persistence/recovery | StateSnapshotStore | Resolution-pending + valuation seeds | persistence | extend |
| Facts/reports | FactEnvelope / JSONL | Z-Gap taxonomy | reporting | extend |
| Calibration | — | Offline/OBSERVE analyzers | research/scripts or reporting | new strategy-specific (analysis) |

---

## C. Dependency and ownership rules

See **§A2.11** for the full allowed-import matrix and **§A2.5** for runtime flow.

### Static dependency direction (summary)

```text
adapters / execution.polymarket  →  core, domain, market_data
indicators (C)                   →  core numerics/ids only
strategies/z_gap (D)             →  core, strategies.{protocol,decisions,context},
                                    indicators (C), domain snapshot types, signals
                                    ✗ adapters, risk, planning, OMS, stores, r7*, reporting writers
risk / planning / execution      →  core, intents  ✗ strategies.z_gap
runtime / application (F)        →  wire concretes; ✗ embed Z-Gap formulas
```

Runtime sequence ≠ import graph.

---

## D. Target contracts

### D.1 Neutral strategy output

**`StrategyDecision`** (shared; not validation-specific)

**F1 generic `action` vocabulary (framework effects only):**

```text
WAIT | SKIP | ENTER | HOLD | EXIT | FLATTEN | BLOCKED
```

| Action | Meaning | Intents (F1) |
|--------|---------|--------------|
| `WAIT` | Not ready; keep sampling | none |
| `SKIP` | Evaluated; no economic change | none |
| `ENTER` | Request long exposure | `EnterIntent` |
| `HOLD` | Keep current exposure; no new intent | none |
| `EXIT` | Become flat (economic/strategy exit) | `ExitIntent` |
| `FLATTEN` | Urgent/shared-risk flatten | `FlattenIntent` |
| `BLOCKED` | Fail-closed; no new risk | none |

| Clarification | Rule |
|---------------|------|
| `EXIT` is the economic **effect** | Rich exit, thesis invalidation, and time exit are distinct **`reason_code`** values — not separate generic actions |
| `FLATTEN` | Urgent/shared-risk path only |
| `STOP` | May appear as a **strategy-level term or `reason_code`**; not required as a distinct generic action if it maps to `EXIT` + `ExitIntent` |
| `HOLD_TO_RESOLUTION` | Full Z-Gap **semantic** preference — **not** an F1 generic action and **must not** cause hidden host lifecycle behavior via evidence inspection |

**F1 `IntentLike`:** `EnterIntent | ExitIntent | FlattenIntent` only.

**F5 resolution hold (explicit request required):**

```text
Strategy chooses resolution hold
→ explicit normalized request (e.g. HoldToResolutionIntent
   or equivalent binary-market lifecycle request — name not frozen in P0)
→ framework validates capability
→ lifecycle → resolution pending
```

The generic host must **not** special-case a Z-Gap-specific decision string. Resolution intent/lifecycle work belongs to **F5**, not F1.

| Field | Meaning |
|-------|---------|
| `action` | F1 vocabulary above |
| `reason_code` | Stable string (e.g. `thesis_invalid`, `market_rich`, `time_flatten`, `kill_switch`) |
| `evidence` | Structured map (edges, \(p\), richness, gates…) |
| `decision_id` / timestamps | Correlation |

### D.2 Atomic decision input (timestamp-consistent evaluation)

`ZGapSignal` + `DecisionContext` is acceptable **only if** both share one decision epoch.

Every strategy evaluation must carry a shared identity containing or referencing:

| Identity element | Purpose |
|------------------|---------|
| Decision / snapshot ID | Single epoch key |
| Evaluation timestamp | When the snapshot was sealed |
| Market / window ID | Binding |
| Causation / correlation ID | Traceability |
| Source / event timestamps | Per-component provenance |
| Freshness state | Stale rejection |
| Lifecycle snapshot version (or equivalent) | Consistency marker |

**Invariant:** never combine model probability from \(T_1\), book from \(T_2\), position from \(T_3\), and \(\tau\) from \(T_4\).

**Allowed shapes (P0 does not freeze class layout):**

- one immutable `ZGapDecisionSnapshot`, or
- `ZGapSignal` + `DecisionContext` sharing one snapshot/epoch identity

Strategy evaluation **must not** fetch mutable stores.

**Planned acceptance tests:**

- mismatched snapshot epochs are rejected;
- stale components cannot silently enter a decision;
- OBSERVE and SHADOW build equivalent decision inputs for the same sealed snapshot;
- strategy evaluation does not access mutable stores directly.

### D.2b Strategy input contents (within one epoch)

Sealed snapshot/context provides:

| Group | Fields |
|-------|--------|
| Time | corrected now, uncertainty_ms, monotonic marks |
| Window | market_id, tokens, start/end, resolution rule id |
| K | `PtbSnapshot` (§D.3) |
| Model | `ModelSnapshot` (§D.4) |
| Books | executable ask/bid/depth/tick/freshness per leg |
| Position | held leg, confirmed qty, cost basis, entry model snapshot |
| Lifecycle | phase, pending entry/exit, exit outstanding, exited_this_window |
| Risk | kill, max-loss breach flags, entry/exit allowed gates |
| Resolution capability | available / unavailable / point-of-no-return (informational until F5 intent) |
| Mode | OBSERVE / SHADOW / (future live denied in generic path) |

No raw OMS events in strategy.

### D.3 PTB / K snapshot

| Field | Notes |
|-------|-------|
| `k` | Decimal or null |
| `window_id` / start/end | Binding |
| `source_class` | e.g. boundary_tick / metadata / log / attested — **not** provider-hardcoded in strategy |
| `source_ts`, `receive_ts` | Event vs receive |
| `boundary_lag_ms` | If applicable |
| `quality_class` | confirmed_canonical / provisional / inferred / late / mismatched |
| `locked` | bool |
| `mismatch` / attestation | diffs, tolerances, block reasons |
| `provenance` | opaque provider evidence ref |

### D.4 Model snapshot

`S`, `sigma`, `tau_s`, `z`, `p_up`, `p_down`, `basis_bps`, readiness, validity flags, jump_guard, timestamps, reject reasons.

### D.5 Entry valuation

Selected leg; executable ask/depth; \(C_{\mathrm{entry,unit}}\); \(E_{\mathrm{settlement}}\); \(E_{\mathrm{repricing}}\); fee/slip assumptions; max acceptable price; both-leg diagnostic; decision reason.

### D.6 Active-position valuation

Held leg + confirmed qty; cost basis; executable exit price convention; \(V_{\mathrm{sell}}\); \(p_{\mathrm{held}}\); liquidation PnL; \(V_{\mathrm{resolve,gross/adj}}\); remaining_hold_edge; market_richness; thesis state; resolution eligibility; decision reason.

### D.7 Four axes (never one enum)

1. Strategy decision action  
2. Order/execution phase  
3. Inventory state (`FLAT` / `FLAT_WITH_DUST` / `RESIDUAL` / `UNKNOWN` / open)  
4. Terminal outcome/provenance  

---

## E. PTB provider strategy

**Strategy requires:** resolution-aligned frozen \(K\) + quality class — not “Chainlink”.

| Mode | K requirement |
|------|----------------|
| OBSERVE | May use provisional/inferred with explicit evidence labels |
| SHADOW | Prefer locked usable K; provisional only if config allows and labeled |
| Future live | Confirmed canonical / attested; mismatch → block |

**Lifecycle of K:** observe → (optional attest) → lock when used for entry → immutable for window → post-window reconcile vs resolution.

**Initial provider:** product decision. Legacy evidence points to boundary capture from settlement-associated reference ticks as a **candidate adapter**, behind the port. F1 proceeds without choosing; F2 needs either a chosen provider or a fixture/synthetic K port for tests.

**Smallest port:** `PtbSource` / capture function → `PtbSnapshot` → lock store. No provider hierarchy.

---

## F. Time model

| Concept | Definition |
|---------|------------|
| Event / source time | Venue or feed timestamp |
| Receive time | Local receipt |
| Corrected current time | TimeAuthority-corrected wall time |
| Monotonic duration | For confirmation intervals |
| Clock uncertainty | `uncertainty_ms` |
| Window membership | source_ts vs event_start for K; end for τ |
| Confirmation durations | thesis confirm, etc. |
| Flatten/commit deadline | before event_end or max hold |
| Evaluation cadence | on book/ref ticks **plus** timer wake |

**Timer recommendation:** host-owned periodic evaluation (or `TimerElapsed` wired to strategy/`evaluate_once`) is **required** for thesis confirmation, time flatten, resolution commit, and quiet markets. Smallest mechanism: host timer → rebuild context → call strategy `on_signal`/`evaluate` with synthetic heartbeat signal — **not** raw `on_execution_event`.

---

## G. Decision engine design

All evaluators below are **Z-Gap-specific pure modules (D)** except where they call **C** helpers. The thin strategy only orchestrates. Hosts never implement these formulas.

| Evaluator | Level | Inputs | Outputs | Cross-call state | Tests |
|-----------|-------|--------|---------|------------------|-------|
| `entry_valuation` | D (+C) | model, quotes, fees | unit costs, edges, max economic price evidence | none | fee signs; E_repricing |
| `position_valuation` | D (+C) | pos view, bid, fees, p_held | V_sell, richness, resolve | none | sell-side signs |
| `readiness` / entry policy | D | snapshot + config | wait/skip/enter desire | none | gate matrix |
| `leg_select` | D | both-leg valuations | selected leg | none | less-probable leg wins |
| `thesis_policy` | D | p_held, thresholds, time | invalid + confirm | ThesisConfirmState | confirm/reset |
| `realization_policy` | D | richness, θ_rich, depth | exit desire | none | market-rich |
| `time_resolution_policy` | D | τ, capability, V_sell vs V_resolve,adj | sell/hold_resolve/continue | optional | sell beats sticky hold |
| `action_precedence` | D | layer flags | StrategyDecision action | none | conflict table |
| Intent mapping | thin strategy | action + evidence | IntentLike list | private epoch/reentry | no venue fields |

Determinism: pure functions of snapshots + explicit confirm state. Time for confirms injected via context.  
Avoid monolith `evaluate_everything()`; avoid one-class-per-gate forest — group as `valuations.py` + `policies.py` (+ thin `strategy.py`).

---

## H. Lifecycle design

### Entry

```text
READY → EnterIntent → Risk → Plan → OMS → ENTRY_PENDING
→ confirmed fill/position → ACTIVE
```

Dedup: semantic_key + one lineage/window. Unfilled: DONE/flat lineage without re-entry if policy consumed attempt — exact rule in config (`entry_attempted`).

### Pre-resolution exit

```text
Exit reason → ExitIntent/FlattenIntent → confirmed qty only
→ fresh exit plan → OMS → EXIT_PENDING → FLAT/DUST/RESIDUAL/UNKNOWN
```

Suppress duplicate exits unless escalate. UNKNOWN: Layer 1 — no blind sell.

### Resolution hold (F5 — explicit request; not F1)

```text
Z-Gap policy prefers resolution hold (among SELL NOW / CONTINUE / HOLD TO RESOLUTION)
→ emit explicit HoldToResolutionIntent (or equivalent; name not frozen in P0)
→ framework validates resolution capability
→ lifecycle RESOLUTION_PENDING
→ (optional point-of-no-return)
→ outcome/payout evidence → portfolio reconcile → resolved terminal
```

Re-compare \(V_{\mathrm{sell}}\) vs \(V_{\mathrm{resolve,adj}}\) every tick until point-of-no-return.  
**Forbidden:** host inspects Z-Gap `reason_code` / evidence and secretly enters resolution-pending in F1–F4.

### Recovery

| Restart during | Behavior |
|----------------|----------|
| Entry pending | Reconcile order; activate or fail closed |
| Active | Resume monitor; restore cost basis + thesis confirm cautiously (reset confirm if unsafe) |
| Exit pending | Resume exit; no second entry |
| Resolution pending | Resume watcher; no discretionary confuse |
| Unknown submission | Block; recon; manual if needed |
| Residual | Block new risk; ops path |

Authoritative evidence: orders/fills/portfolio + venue when available. Persistence fingerprint as today.

---

## I. Risk design

| Concern | Owner |
|---------|-------|
| K quality, σ/model readiness, basis, Z-Gap edge, thesis, richness, no-reentry | **Z-Gap policy (D)** — not RiskEngine |
| Max notional, max liquidation loss, exposure, kill, liquidity/spread bounds, account readiness, unknown inventory, operational safety | **Shared risk (A)** |
| Orders, qty, plans, settlement, inventory class | **Framework (A)** |

Do **not** place Z-Gap formulas inside `RiskEngine`.  
Do **not** place shared loss/exposure authorization inside `ZGapStrategy`.

`LIVE_TINY` remains denied on generic RiskEngine path until a future dedicated live host.

---

## J. Fee and P&L truth

| Name | Meaning |
|------|---------|
| max fee reservation | Bound for collateral |
| estimated entry/exit fee | From φ(`fd`) at assumed price |
| confirmed actual entry/exit fee | From fill/venue when present |
| all-in cost basis | Uses confirmed fees when known else estimated (labeled) |
| gross liquidation PnL | \(V_{\mathrm{sell}}-C\) using stated fee basis |
| estimated net / confirmed realized net | Never conflate |
| expected resolution value | \(V_{\mathrm{resolve,gross/adj}}\) |
| resolution payout | Actual |
| unknown fee state | Explicit |

Rounding/Decimal: venue tick + money step owned by planning/execution; strategy valuations use Decimal with documented quantization.

---

## K. Facts, reports, and calibration

**Reporting separation:** `ZGapStrategy` returns structured evidence only. Host/reporting converts snapshots, valuations, decisions, intents, risk results, plans, execution events, and lifecycle changes into append-only facts. **Do not recompute** fair value, P&L, or market richness inside reporting — persist valuation outputs from the decision evidence.

### Minimum fact types (names illustrative)

market/window selected · resolution rule · K observe/lock/mismatch/attest · time-authority · model warm/ready · σ snapshot · FV snapshot · entry eval + every reject · entry valuation · intent/risk/plan/order/fill · position valuation · thesis state · market_richness / remaining_hold_edge · hard-risk exit · strategy exit · resolution eligibility/selection · exit plan/fill · inventory terminal · estimated vs confirmed fees · per-window outcome

### Calibration outputs

reliability/Brier · edge buckets · fillability · bid vs model decomposition · PnL by exit family · round-trip friction · sell-vs-resolution regret · threshold sensitivity · basis/K mismatch · missed opportunity / false entry

| Mode | Available |
|------|-----------|
| Offline fixtures | Full decision + simulated books |
| OBSERVE | Decisions + counterfactual valuations; no orders |
| SHADOW | Full simulated lifecycle PnL (model fees) |
| Future live | Confirmed fees/payouts when venue reports |

---

## L. Configuration design

Versioned `z_gap` config (YAML/JSON) sections:

| Section | Contents | Binding |
|---------|----------|---------|
| `model` | σ EWMA, τ_floor, jump | → estimator |
| `entry` | θ_take, z/p bands optional, τ band, basis, slip ticks, one_position, E_repricing required flag | → entry_decision |
| `exit` | θ_rich, p_stop, confirm_s, flatten_before_end, max_hold | → evaluators |
| `resolution` | enabled, qualify thresholds, penalty placeholders | → time_resolution_eval |
| `risk` | max_usd, max_liquidation_loss, kill | → risk + strategy |
| `ptb_time` | lag max, mismatch bps, uncertainty max by mode | → readiness (bound to code) |
| `mode_capability` | observe/shadow/resolution flags | → host |
| `reporting` | fact verbosity, calibration sample | → sink |

Mark legacy numeric defaults **provisional**. **No dead keys** (e.g. unused clock_drift if N/A — omit or wire).

---

## M. Testing strategy

### Pure math

EWMA · jump · Φ(z)/p · entry edges · **sell-side signs** · fee signs · V_resolve · p↔z equivalence

### Golden parity

Copy trusted vectors from `old/tests/fixtures/z_gap/fair_value_golden.json` into `tests/fixtures/z_gap/` with provenance note — **do not import old/**. Recreate independently only if legal/clarity requires; prefer copy-with-attribution for numeric lock.

### Decision tests

Less-probable leg selected · both-leg ambiguity reject · E_repricing reject · thesis p_stop · market-rich exit · hard loss · deadline · sell vs resolve · hold qualify without sticky suppress · precedence conflicts · UNKNOWN no blind sell

### Lifecycle tests

Entry→active · rich exit→flat · thesis exit · risk flatten · time exit · no dup exits · no re-entry · partial fill policy · residual/unknown · restart · resolution-pending sim (F5)

### Mode tests

OBSERVE no orders · SHADOW full path · resolution hold gated · LIVE denied

### Architecture tests

Full list in **§A2.12**. Minimum: no `old/`; no `z_gap`→adapters/OMS/risk/planning/stores/r7*; no risk/planning/execution→`z_gap`; protocol free of validation import; hosts free of Z-Gap formulas; single decision entrypoint for OBSERVE+SHADOW; valuation ownership single-home; reporting no recalculation; ReferenceMomentum green.

Each milestone lists exact accepting tests (§O).

---

## N. Migration and compatibility

| Topic | Plan |
|-------|------|
| ReferenceMomentum | Keep working: move `ObserveDecision` to neutral module or adapter typedef; widen protocol to IntentLike matching its real returns |
| Existing OBSERVE/SHADOW | Behavior preserved for momentum configs; Z-Gap is additive strategy kind/wiring |
| Test updates | Protocol/architecture tests expect new types; momentum e2e unchanged in spirit |
| Accidental live | No new live commands; RiskEngine still denies LIVE_TINY |
| Legacy formulas | Port with provenance comments + goldens; never `import old` |

---

## O. Implementation sequence (F1–F5 + Future)

```text
F1  Generic decisions, contexts, economics types, lifecycle axes clarity
F2  Resolution rule, PTB/K port, time authority, reference alignment,
    σ/FV, pure entry/exit valuation (+ goldens)
F3  Full OBSERVE decision engine + calibration facts
    (counterfactual sell/resolve valuations)
F4  SHADOW pre-resolution lifecycle
    entry → rich/thesis/risk/time exit → flat
F5  Resolution-aware offline/SHADOW
    eligibility, simulated payout, regret, resolution-pending recovery
Future  Authorized live + real settlement/redeem (separate)
```

### F1 — Generic contracts (architecture-first)

| Field | Content |
|-------|---------|
| Objective | Neutral protocol + IntentLike + generic economics **value types**; keep momentum working; **no Z-Gap policy yet** |
| Functionality | `StrategyDecision` with F1 actions `WAIT\|SKIP\|ENTER\|HOLD\|EXIT\|FLATTEN\|BLOCKED`; IntentLike = Enter/Exit/Flatten only; relocate ObserveDecision; optional `core/economics` types; architecture import tests |
| Modules | `strategies/{protocol,decisions}.py`, `framework_validation/*`, maybe `core/economics.py` |
| Contracts | D.1 (generic actions); J naming |
| Migrations | Protocol widen; momentum returns IntentLike officially |
| Tests | §A2.12 subset; action enum has **no** `HOLD_TO_RESOLUTION`; momentum e2e unchanged in spirit |
| Acceptance | Protocol free of validation import; IntentLike typed; no resolution lifecycle from F1 actions |
| Exclusions | No PTB, σ, Z-Gap strategy, host Z-Gap wiring, no resolution intent |
| Risks | Leaking resolution into generic actions — **forbid** |
| Stop | Review before F2 |
| **P0 freeze** | F1 framework-generic only; Z-Gap valuations deferred to F2 |

### F2 — Inputs (E/A/B) + quantitatively reusable math (C) + Z-Gap valuations/policies (D) pure

| Field | Content |
|-------|---------|
| Objective | Offline-testable chain: fixture K/time/S/books → C indicators → D valuations/policies — **no thin strategy host productization required**, but pure API stable |
| Functionality | TimeAuthority fake; PtbSnapshot+fixture provider (E→B); EWMA+FV (**C**); `strategies/z_gap/valuations.py` + `policies.py` (**D**); sell-sign + leg-select tests |
| Modules | `indicators/*`, `domain` K/resolution types, `strategies/z_gap/{valuations,policies,reasons,config}`, adapters fixtures only under tests |
| Contracts | D.3–D.6; §A2.9 |
| Tests | goldens; sell signs; less-probable leg; E_repricing; policy unit tests; **import firewall for z_gap** |
| Acceptance | Policy/valuation importable without adapters/OMS; goldens match |
| Exclusions | No OMS; no generic host Z-Gap formulas; thin `strategy.py` optional stub OK |
| Risks | Leaking policy into indicators — keep thresholds in D |
| Stop | Review ownership + math |
| **Arch correction** | Explicit C vs D split; valuation not in host |

### F3 — OBSERVE composition (F): same pure decision path, facts, no orders

| Field | Content |
|-------|---------|
| Objective | Composition root wires stores→indicators→snapshot→**thin ZGapStrategy**→facts; OBSERVE OMS off |
| Functionality | Thin `strategy.py`; snapshot assembler; timer/heartbeat; fact emission from decision evidence (**no recalculation**); config |
| Modules | runtime observe wiring (**no formulas**), reporting, `strategies/z_gap/strategy.py` |
| Tests | host has no Z-Gap math; observe invokes same pure entrypoint as unit tests; no SubmitOrder |
| Acceptance | Decision parity with F2 pure tests; facts carry valuation outputs |
| Exclusions | ShadowOMS; resolution commit |
| Stop | Review |
| **Arch correction** | Host is F-only; strategy thin |

### F4 — SHADOW composition: identical decisions, OMS on

| Field | Content |
|-------|---------|
| Objective | Same decision functions as F3; intents→risk→plan→ShadowOMS→flat |
| Functionality | Economic intents only; planner owns qty/price/style; lifecycle dup suppression; persistence of framework + minimal strategy slice |
| Modules | shadow_host composition, lifecycle, retry |
| Tests | e2e; no venue fields on strategy intents; UNKNOWN no blind sell; shared decision entrypoint with F3 |
| Acceptance | Terminal inventory honest |
| Exclusions | Resolution hold enable; live |
| Stop | Review |
| **Arch correction** | Explicit same-decision-path test F3↔F4 |

### F5 — Resolution-aware (still no live redeem)

| Field | Content |
|-------|---------|
| Objective | Explicit resolution request + lifecycle; simulated payout; regret; recovery |
| Functionality | Introduce `HoldToResolutionIntent` (or equivalent); capability validation; PONR sticky only; no host special-casing of Z-Gap evidence |
| Tests | sell beats hold; intent required for resolution-pending; epoch consistency; architecture still holds |
| Exclusions | Real redeem/live |
| Stop | Review before live design |
| **P0 freeze** | Resolution is an explicit normalized request in F5 — not an F1 evidence-only action |

### Future — Live

Separate authorization, settlement truth, redeem, operator tooling. Not part of F1–F5.

---

## P. Dependency graph and critical path

```text
F1 ──► F2 ──► F3 ──► F4 ──► F5 ──► Future live
         │
         └── PTB provider choice (can use fixtures until chosen)
```

| Parallelizable after F1 | Notes |
|-------------------------|-------|
| Pure σ/FV port vs TimeAuthority fake | Yes |
| Fact schema draft vs math | Yes |
| Docs/config schema vs code | Yes |

| Blocks | What |
|--------|------|
| F1 acceptance | Protocol shape |
| F2 provider (or fixtures) | Real K integration tests |
| F3 | F2 pure parity |
| F4 | F3 decision stability |
| F5 | Explicit enable resolution capability |
| Live | Entire F4/F5 + ops decisions |

**Smallest vertical slice:** F1 neutral protocol → F2 pure C+D valuation/policy on fixtures → one F3 observe tick through **thin** `ZGapStrategy` emitting SKIP/ENTER facts (no OMS). Proves thin orchestrator without embedding math in the host.

### Architecture-review impact on milestones

| Change | Effect |
|--------|--------|
| Thin strategy budget | F3 introduces `strategy.py`; F2 focuses on pure D modules |
| C vs D split | EWMA/FV are F2 **indicators**, not “strategy-owned estimators” |
| No host formulas | F3/F4 acceptance includes host AST/import guards |
| Same decision path | Explicit F3↔F4 shared entrypoint test |
| Reporting no recalculation | F3+ fact schema stores valuation outputs |

---

## Q. Open decisions

### Must decide before F1

| ID | Topic | Recommendation |
|----|-------|----------------|
| P1 | Accept §0 corrections A–F as frozen economics | **Accept** |
| P2 | StrategyDecision + IntentLike protocol approach | **Accept** as in D.1 |
| P0 | Accept architecture review: thin ZGapStrategy + C/D split + no host formulas | **Accept** (§A2) — **required before F1** |

### Must decide before F2

| ID | Topic | Recommendation |
|----|-------|----------------|
| P3 | Initial K provider for non-fixture runs | Candidate: settlement-associated boundary tick adapter behind port; **fixtures OK to start** |
| P4 | Settlement-associated reference feed for basis | Keep dual-reference design |

### Must decide before F3/F4

| ID | Topic | Recommendation |
|----|-------|----------------|
| P5 | Default θ_rich / p_stop / flatten deadline for evidence | Use provisional legacy-mapped values; label provisional |
| P6 | Entry attempt consumption on unfilled FAK | Fail closed: consume lineage (no retry storm) unless retry policy explicit |

### Provisional during calibration

All numeric thresholds; resolution penalty magnitudes; τ bands; basis max; slip ticks.

### Future live only

Mutation auth; real redeem; capital; ack/residual operator flows; continuous live host.

---

## Consistency notes

| Source | Relationship |
|--------|--------------|
| Phase A audit | Evidence for math goldens & gates; **not** identity; sell-side legacy formula superseded by Correction A |
| Full spectrum §0 | Economic truth for this plan |
| Docs/latest R8 | Host/OMS/authority model reused; protocol debt fixed in F1; Z-Gap must not import r7* |

---

## Verification (authoring)

```text
python -m pytest tests -q --tb=no
git status
git diff -- Docs/implementation/
```

No commit/push. No code under `src/`.
