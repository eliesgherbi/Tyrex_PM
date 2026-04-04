# Phase A closure — Nautilus guru path (Tyrex_PM)

**See also:** [`current_state.md`](current_state.md) — maintainer hub and roadmap mapping. **Phase B:** B0 validation; B1 `portfolio_exposure`; **B2** portfolio cap; **B3** guru concurrent resting-order cap; **B4** reserve on py-clob balance; **B5** docs + startup gate summary (`phase_b_startup.py`, `OPERATIONS.md` § Phase B) — see `Phase_B_planing.md`. **Pre–Phase C:** live-session risks (restarts, marks, denial rates) — [`phase_b_operational_validation.md`](phase_b_operational_validation.md).

This note records **what Tyrex actually reads and enforces** after the Phase A closure work, and what remains **partial** or **blocked** by Nautilus / the Polymarket adapter.

---

## 1. Open orders / pending exposure (Workstream A)

**Semantics:** Nautilus initializes `Order.leaves_qty` equal to `Order.quantity` and **decrements `leaves_qty` on each fill** while keeping `quantity` as the original order size (*package-source-confirmed:* `nautilus_trader.model.orders.base` / `base.pyx`).

**Tyrex:** `OrderSnapshot.leaves_quantity` mirrors `order.leaves_qty`. `ConfiguredRiskPolicy._pending_open_notional_from_cache` computes resting notional as **Σ (leaves_quantity × price)** for open orders matching the guru outcome `token_id`.

**Terminal orders** (fully filled / canceled) should not appear in `Cache.orders_open()`; they do not contribute to pending exposure.

**Tests:** `tests/unit/test_configured_risk.py`, `tests/test_state_readers.py`, `tests/test_phase_a_risk.py`.

---

## 2. Filled exposure / positions (Workstream B)

**Definition:** “Position truth” here means **`Portfolio.net_exposure(instrument_id, price=mark)`** in **account cost currency** (typically USDC for Polymarket), using the guru / intent reference price as **mark** when gating new follows.

**Runtime:** `NautilusPositionStateReader` in `src/tyrex_pm/runtime/state_readers.py` resolves `token_id` → `InstrumentId` via YAML map + **cache-only** scan (same outcome-token matching as dynamic activation, without HTTP). It does **not** import `CopyStrategy`.

**Risk:** When `max_token_notional_usd_open` is finite and the framework-submit path is active, the per-token cap uses **`filled_exposure + pending + new_order`**. If `fail_on_unresolved_position_for_token_cap` is true and `net_exposure` cannot be computed (`None`), the intent is **denied** (`RISK_POSITION_EXPOSURE_UNRESOLVED`). Default is fail-open on unresolved (filled treated as 0) to avoid blocking first trades in edge cache states.

**Blocked / adapter-dependent:** Position correctness requires the **Polymarket adapter + user/exec channel** to emit position events so `Portfolio` matches venue holdings. Tyrex only **reads** `Portfolio`; it does not reconcile positions itself.

---

## 3. Capital truth (Workstream C)

**Pre-trade contract:**

| Source | Role |
|--------|------|
| `NautilusAccountSnapshotProvider` | `Portfolio.account(POLYMARKET)` must be **present** when `capital_gate_enabled` is true. |
| `ClobAllowanceStateProvider` | When `min_collateral_balance_usd` or `min_allowance_usd` is set, py-clob **`balance`** / **`allowance`** strings are parsed as floats and compared. |

**Staleness:** Cached snapshots are **refreshed** when their `captured_at_utc` age exceeds `max_account_snapshot_age_seconds` or `max_allowance_snapshot_age_seconds` (allowance cache only used when a minimum is configured). If a **required** read cannot satisfy the gate (missing provider, `account_present` false, unparseable balance/allowance when a min is set, or **B4** unparsable `balance` when reserve > 0), evaluation **fails closed**.

**Config:** See `config/risk/guru_follow_risk.yaml` (commented example) and `RiskSettings` in `src/tyrex_pm/config/loaders.py`.

---

## 4. Restart / reconciliation (Workstream D)

**Tyrex `TradingNodeConfig`:** `load_state=False`, `save_state=False` — **no** persisted Nautilus trader state from Tyrex.

**Expected after boot:**

| Store | Expected content |
|-------|------------------|
| `Cache` (instruments/orders) | Seeded from `InstrumentProviderConfig.load_ids`, **dynamic activation**, optional guru **warmup**, then **adapter** connection / reconciliation. |
| `Portfolio` | Updated when the adapter pushes account / position events — **timing is adapter-defined**. |
| Venue truth | Arrives over HTTP / WS as implemented in **nautilus_trader** Polymarket integration. |

**Zero-bootstrap:** Outcome tokens unknown at start may be **missing** from `Cache` until warmup or first guru-driven resolve. Risk may under-count filled exposure until the instrument exists in `Cache` (unless `fail_on_unresolved_position_for_token_cap` is on).

**Warnings:**

| Signal | Usually |
|--------|---------|
| “Instrument not found” on historical mass reports | **Observability** if the instrument was never loaded. |
| `orderbook … does not exist` from CLOB | **Venue / market** issue. |
| RiskEngine notional vs balance | **Sizing / limits** vs real balance. |

**Narrow Tyrex improvement shipped:** `run_guru.py` prints a **Phase A** one-liner when live + Nautilus + framework submit, pointing operators at this doc.

---

## 5. Phase A checklist (Tyrex-observable)

| Criterion | Status | Notes |
|-----------|--------|--------|
| Open orders queryable via framework path | **Complete** | `NautilusExecutionStateReader` → `Cache.orders_open` when live Path A + guru uses framework submit. |
| Fills / cancels update local `Cache` semantics used by Tyrex | **Partial** | Tyrex does not handle events; relies on adapter. **Pending** math is **correct** w.r.t. `leaves_qty`. |
| Positions visible to risk from framework state | **Partial** | `NautilusPositionStateReader` + `net_exposure`; correctness **blocked** if adapter does not feed `Portfolio`. |
| Restart / reconciliation story | **Partial** | No Tyrex `load_state`; post-restart truth = **venue + adapter**. Documented; not fully verifiable inside Tyrex alone. |
| Account / balance snapshot | **Complete** (read path) | `NautilusAccountSnapshotProvider`; optional **gate** when `capital_gate_enabled`. |
| Allowance / approval | **Complete** (read path) | `ClobAllowanceStateProvider`; optional mins when gate + thresholds set. |
| Stale snapshot → fail closed | **Complete** | TTL-driven refresh; missing / insufficient → deny when gate enabled. |

---

## 6. Blocked upstream (not fixable in Tyrex alone)

- **Order / fill / cancel event ingestion** and **portfolio reconciliation** semantics for Polymarket.
- **Account / position** timeliness after reconnect without adapter guarantees.
- **Enabling `load_state` / `save_state`** for durable Nautilus state: Tyrex keeps them **off** until product verifies compatibility with Polymarket live.

---

## 7. Files touched (implementation index)

- `src/tyrex_pm/runtime/state_readers.py` — `leaves_quantity`, `instrument_id_for_outcome_token`, `NautilusPositionStateReader`
- `src/tyrex_pm/risk/configured.py` — pending leaves, filled + capital gate
- `src/tyrex_pm/config/loaders.py` — `RiskSettings` Phase A fields
- `src/tyrex_pm/core/reason_codes.py` — new risk reason codes
- `src/tyrex_pm/runtime/guru_compose.py` — wire `position_state` / `position_reader`; **B5** Phase B INFO summary line
- `src/tyrex_pm/runtime/phase_b_startup.py` — **B5** formatted `tyrex_pm phase_b:` startup string (no risk behavior)
- `scripts/run_guru.py` — Phase A boot line
- `config/risk/guru_follow_risk.yaml` — commented contract
- Tests: `tests/test_phase_a_risk.py`, updates to `test_state_readers`, `test_configured_risk`, `test_guru_compose_build`, `test_split_config_loaders`
