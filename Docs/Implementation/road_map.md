# Tyrex_PM — archived migration roadmap (Nautilus-first)

> **This document is historical context, not the primary description of current behavior.**  
> For how the system works **today**, read [**Architecture.md**](../Architecture.md), [**Implementation/current_state.md**](current_state.md), and [**OPERATIONS.md**](../OPERATIONS.md). Those use functional language aligned with the codebase.

The text below records **original phased planning** (framework state first, then deployment-based risk, then follow/execution refinements). Many items are **already implemented**; treat the snapshot table as a **rough progress marker**, not a spec.

**Principle (still valid):** Maximize **NautilusTrader** for live trading state (cache, portfolio, orders, reconciliation). Strategy stays thin; **risk** and **runtime** consume framework-visible state.

---

## Historical baseline (reference — pre–Path A default)

**Historical baseline (removed from the operator platform):** a node with **empty** `data_clients` / `exec_clients` and live guru orders via **py-clob HTTP** beside Nautilus. **Current Tyrex:** **`execution_mode: live`** always registers Polymarket Nautilus clients and submits via **`NautilusGuruExecutionPort`**; obsolete YAML keys `polymarket_nautilus_live` / `polymarket_framework_submit` are rejected at load.

**Operational note:** Many current guru deployments instead enable **Path A** (Polymarket Nautilus data + exec + optional framework submit). Do **not** assume an empty kernel when reading older notes—see **`current_state.md`** for the live matrix.

---

## Implementation snapshot (historical labels vs code)

**Current detail:** [`current_state.md`](current_state.md).

| Roadmap theme (historical name) | Status in code (high level) |
|--------------------------------|-----------------------------|
| Framework-first live state | **Largely done:** guru orders via **`NautilusGuruExecutionPort`**; readers; deployment-budget math; optional capital gate; lifecycle via Nautilus + adapter. **`load_state=False`** in compose. |
| Product risk on framework truth | **Done:** deployment caps, concurrent guru rests, reserve, compose startup summary line. **Backlog:** e.g. cooldowns, per-cycle follow caps — not in code unless documented elsewhere. |
| Ingest + sizing + execution quality | **Done:** RTDS + poll + gap-fill; optional conviction sizing; optional book guard / depth clip / limit timeout on live submit. **Backlog:** broader pacing / TWAP — design-only unless coded. |

---

## Phase A — First priority: Nautilus-native live trading state

**Goal:** Make the **Nautilus cache / portfolio / order machinery** the place strategy-adjacent code (especially **risk**) consults for truth: open orders, lifecycle events, fills, positions, and restart reconciliation.

### A1. Integrate Polymarket execution state into Nautilus

**Intent:** Wire a real **execution client** (and supporting adapters) so submissions and venue feedback flow through paths that update **local framework state**.

**Minimum success criteria:**

| Criterion | Meaning |
|-----------|---------|
| Open orders queryable | After submit, **open orders** are visible via the **framework path** (cache / execution engine APIs), not only via ad-hoc SDK lists. |
| Fills / cancels propagate | **Fills** (including partial) and **cancellations** update local state **automatically** through Nautilus events or registered handlers. |
| Positions visible | **Positions** (or venue-equivalent holdings) are visible to **risk** from **framework state**, not from a parallel private dict alone. |
| Restart / reconciliation | On **restart**, state is **reconciled** with the venue (or restored from durable Nautilus state) so risk does not start from zero blindly. |

**Out of scope for A1:** New follow-policy knobs; those belong in **Phase C**.

### A2. User / order WebSocket (high priority)

**Intent:** Add a **live order-state feed** (user WebSocket or equivalent) so order updates are **event-driven** and **timely**.

**Rationale:** Without this (or equivalent streaming reconciliation), follow-control and risk rules operate on **stale or inferred** order state. That weakness is more damaging than adding fancy pacing on the current architecture.

**Success criteria (directional):**

- Order state transitions (ack, resting, partial fill, full fill, cancel, reject) are reflected in framework-visible state within bounded latency.
- Risk can distinguish **pending** vs **filled** exposure using that view (exact API shape TBD by Nautilus + adapter design).

### A3. Account / balance snapshot integration

**Intent:** Provide a **local capital view** for fail-closed gating, even if some fields remain Polymarket-specific under the hood.

**At minimum:**

- **Available USDC / buying power** (or closest venue equivalent) as a **timestamped snapshot**.
- **Allowance** (or approval state) where needed for trading—via venue-specific calls if Nautilus has no first-class model.
- **Stale snapshot → fail closed** for new risk-on intents (configurable thresholds).

**Boundary:** If Nautilus cannot carry allowance natively, use a **small adapter/ledger**—owned by **runtime + risk**, **not** embedded in strategy logic.

---

## Phase B — Second priority: pending- and position-aware risk

**Precondition:** Phase A success criteria substantially met (framework order/position/fill visibility + reconciliation story).

**Intent:** Evolve **risk** from:

- `price_ref * quantity` estimates and **session-only** exposure heuristics (historical pre–Path A)

to:

- **Pending notional** from **live open orders** (framework-backed).
- **Filled / position** exposure from **positions**.
- **Available balance** and **reserve** rules using **Phase A** snapshots.
- **Per-token and portfolio** caps grounded in that state.
- **Max concurrent follow attempts** (as a risk/runtime constraint, not strategy clutter).
- **Cooldown / burst** caps where they depend on real exposure.

**Relationship to older “execution control” notes:** The ideas (pending caps, reserve collateral, pacing) remain valid but should be **implemented against real state** from Phase A—not duplicated as guessed counters.

---

## Phase C — Third priority: venue normalization + follow policy controls

**Precondition:** Phase B in place (or at least pending/position/balance inputs reliable).

**Intent:** Add controls that are **correct** and **durable** because venue and book state are real:

- **Venue order normalization** (tick size, min size, min notional handling modes).
- **Follow policy:** max follows per poll cycle, per-token cooldown, repeated-buy rules, pending-order suppression, burst prioritization.
- **Execution** owns normalization details; **signal/policy** owns “whether to attempt follow” rules; **risk** owns capital and exposure gates.

This is where fine-grained copy behavior belongs **without** reworking core state again.

---

## Concrete migration steps (ordered)

These steps align engineering work with the phases above.

### Step 1 — Audit Nautilus + Polymarket adapter surface

Inventory what **NautilusTrader** and any existing **Polymarket** integration already expose:

- Execution client lifecycle, order commands, report/event types.
- **User** / private WebSocket (or official streaming) for **own** order updates.
- **Order cache**, **portfolio**, position update hooks.
- **Account / margin / balance** (or stubs) and extension points.
- **Reconciliation** APIs, startup sync, idempotency.

**Deliverable:** Short internal matrix: *capability → Nautilus API → gap for Polymarket*.

### Step 2 — Refactor runtime wiring (`TradingNode` is not “empty”)

**Intent:** Stop treating `TradingNode` as a shell with all trading I/O in a side-channel policy.

- Register **real** `data_clients` / `exec_clients` (as appropriate to Nautilus version and Polymarket adapter).
- Route **order submission** through the pathway that **updates cache/portfolio** (adapter bridge from current `OrderIntent` / py-clob if needed—but **one** source of truth).

**Deliverable:** Guru compose path builds a node where **submitted orders** are visible through Nautilus’ normal introspection.

### Step 3 — Shared runtime / service layer for live state (risk consumes; strategy does not own)

**Intent:** Centralize reads of positions, open orders, and account snapshots for **risk** (and logging).

- Strategy remains: signal → size → **risk** → submit.
- **Risk** (and optional `CapitalLedger` / `ExecutionStateReader`) queries **framework + adapter**, not strategy-local globals.

**Deliverable:** Interface(s) for “current portfolio/order/balance view” used by `ConfiguredRiskPolicy` successor.

### Step 4 (roadmap) — Implement Phase B controls on top of real state

**Naming note:** This is **roadmap Step 4** (Phase B delivery). Engineering uses a separate sequence: **engineering Step 4** in `step_4_runtime_integration.md` is **framework guru submit** + cache-aligned pending exposure—not the full Phase B list below.

Only after Path A truth is operationally trusted:

- Pending-aware and position-aware limits **beyond** the current minimal token cap.
- Reserve collateral / available-balance gates **beyond** optional `capital_gate_enabled`.
- Concurrent follow caps, cooldowns, burst limits **tied to measured exposure**.

### Step 5 (roadmap) — Guru ingestion and latency (optional / later alignment)

Revisit whether guru activity stays **poll-based** or moves toward **lower-latency, event-driven** inputs consistent with the broader platform spec (WebSocket-first market data, stronger engine integration). This is **not** a blocker for Phase A–B if poll + user-WS is sufficient for v1 risk correctness.

**Naming note:** **Engineering Step 5** (`step_5_runtime_integration.md`) is **dynamic instrument resolution / zero-bootstrap**—orthogonal to this roadmap step’s ingestion/latency focus.

---

## What not to do next (explicit)

- **Do not** implement large sets of **copy policy knobs** (per-cycle caps, cooldowns, pending suppression) **without** measured **Cache / Portfolio** exposure from the **Nautilus** path.
- **Do not** push **allowance/balance/order book-keeping** into **`CopyStrategy`**; keep it in **runtime + risk** with framework-backed sources.
- **Do not** treat **venue rejects** as the primary risk layer once Phase A is done—**pre-trade** gates should use local truth + fail-closed staleness.

---

## Phase summary table

| Phase | Focus | Depends on |
|-------|--------|------------|
| **A** | Nautilus-native orders, user WS, account snapshots, reconciliation | Current baseline |
| **B** | Risk: pending, positions, balance reserve, real caps | Phase A |
| **C** | Venue normalize + follow policy / pacing / suppression | Phase B (state real) |

---

## Document history

- **`road_map.md`** (this file): Nautilus-first phased migration; **replaces** the previous `execution_control_improvements.md` sequencing. Detailed workflow/risk maps from that earlier doc may be recovered from version control if needed for implementation detail.
- **2026:** Added **implementation snapshot** and **naming notes** so roadmap Phase/Step labels are not confused with engineering milestone files (`step_*_runtime_integration.md`). See **`current_state.md`**.
