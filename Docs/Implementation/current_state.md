# Tyrex_PM — current implementation state (maintainer hub)

**Purpose:** Single place to see what the **codebase actually does** today, how that maps to `road_map.md` phases, and where behavior is **complete / partial / blocked / transitional**.  
**Evidence:** `src/tyrex_pm/`; detail: `phase_a_closure.md`, step `*_runtime_integration.md` notes.

---

## 1. Architecture (as implemented)

| Layer | Responsibility |
|-------|----------------|
| **`data/`** | Guru poll → `GuruTradeSignal` on bus. |
| **`strategy/`** | `CopyStrategy`: policies → `OrderIntent` → **`risk.evaluate`** → **`execution.submit_intent`**. **No** `Cache` / `Portfolio` imports (guarded by tests). |
| **`risk/`** | `ConfiguredRiskPolicy`: pre-trade gates using **`RiskSettings`** + injected **runtime readers** (see below). |
| **`execution/`** | `ExecutionPort`: **`NoOpExecutionPort`** (shadow), **`PolymarketExecutionPolicy`** (legacy live py-clob), **`NautilusGuruExecutionPort`** (live framework submit when configured). |
| **`runtime/`** | `build_guru_trading_node`: `TradingNode`, factories, **`GuruTradingAssembly`** (node, risk, readers). |
| **`runtime/state_readers.py`** | Canonical **read boundary** for Nautilus `Cache` / `Portfolio` and py-clob allowance (not from strategy). |

**Live paths (choose one submit path per deployment):**

| Mode | `TradingNode` clients | Submit | Pending exposure for token cap | Filled exposure for token cap |
|------|----------------------|--------|-------------------------------|-------------------------------|
| Shadow | Optional empty | `NoOpExecutionPort` | N/A | N/A |
| Live legacy | Often empty | py-clob `PolymarketExecutionPolicy` | Session `_token_open` after HTTP OK | Not framework-backed |
| Live Path A + framework | Polymarket data + exec | `NautilusGuruExecutionPort` → `submit_order` | **`Cache` open orders, leaves qty × price** | **`Portfolio.net_exposure`** via `NautilusPositionStateReader` (when wired) |

Instrument resolution (framework path): **`GuruInstrumentDynamicController`** (Gamma + CLOB + `Cache` activation) with optional YAML **`polymarket_token_to_instrument`** overlay; **zero-bootstrap** = empty `polymarket_instrument_ids` + live Nautilus + framework submit (implicit dynamic). See `step_5_runtime_integration.md`.

---

## 2. Roadmap mapping (honest)

| Phase (road_map.md) | In Tyrex | Still adapter / venue dependent |
|---------------------|----------|----------------------------------|
| **A** — Nautilus-native state | **Partial → largely closed:** framework submit, readers, pending **leaves**, position reader + risk, optional **capital gate** with TTL refresh. | Order/fill/position **event** delivery and post-reconnect truth are **Nautilus + Polymarket adapter**; `load_state=False`. |
| **B** — Pending/position-aware risk *product* | **B0–B5 complete** (see `Phase_B_planing.md` §10). **Pre–Phase C:** validate real sessions per **`phase_b_operational_validation.md`** (restart, marks, denial semantics). | **Phase C** (follow policy / venue normalize — plan §13); alternate exposure scalars only via explicit ADR. |
| **C** — Follow policy / venue normalize | **Intentionally deferred** (cooldowns, per-cycle caps, suppression). | — |

**Roadmap “Concrete steps” vs engineering:** The numbered **Step 1–5 milestone docs** under `Docs/Implementation/` describe **engineering deliveries** (audit, wireup, readers, framework submit, dynamic instruments). **`road_map.md` § “Step 4/5”** uses different labels (Phase B / guru ingestion). Cross-reference this file when reading the roadmap to avoid conflating the two numbering schemes.

---

## 3. Configuration quick reference

- **Runtime:** `polymarket_nautilus_live`, `polymarket_framework_submit`, `polymarket_instrument_ids` (optional empty with framework submit), `polymarket_dynamic_instruments`, caps, Gamma URL, `polymarket_startup_token_warmup_max`. See `CONFIG_MODEL.md` and `config/runtime/live_polymarket.yaml` comments.
- **Risk:** capital gate + Phase B B2–B4 fields (`max_portfolio_notional_usd_open`, `fail_on_unresolved_portfolio_exposure`, `max_concurrent_guru_resting_orders`, `collateral_reserve_usd`, …). **Supported modes / invalid combos:** `OPERATIONS.md` § Phase B; load/compose validation per `Phase_B_planing.md` §7. See `config/risk/guru_follow_risk.yaml` and `CONFIG_MODEL.md`.
- **Secrets:** `.env` only; never YAML.

---

## 4. Operational failure classes (where to look)

| Symptom | Typical cause | Tyrex vs venue |
|---------|---------------|----------------|
| `copy_skip` + `risk_denied` | `ConfiguredRiskPolicy` | **Tyrex** — see reason string / `ReasonCode`. |
| `RISK_INSUFFICIENT_*`, `RISK_ACCOUNT_UNAVAILABLE`, `RISK_INSUFFICIENT_FREE_COLLATERAL_AFTER_RESERVE` | Capital gate / B4 reserve | **Tyrex** when `capital_gate_enabled` (reserve needs py-clob balance snapshot). |
| `RISK_PORTFOLIO_NOTIONAL_CAP_EXCEEDED`, `RISK_PORTFOLIO_EXPOSURE_UNRESOLVED`, `RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT` | Phase B B2 / B3 | **Tyrex** framework path — see `OPERATIONS.md` § Phase B reason table. |
| `TYREX_MIN_BUY_NOTIONAL_USD` skip / small BUY dropped | Env floor in execution ports | **Tyrex** (both legacy and Nautilus port enforce min BUY notional). |
| CLOB min size / tick / RiskEngine notional | Venue + Nautilus engine | **Venue / adapter**; tune size and risk YAML. |
| `orderbook … does not exist` | No book for token / market | **Venue** — not a Tyrex cache bug by itself. |
| `GURU_INSTRUMENT_*`, `GURU_DYNAMIC_*` | Resolve / cache / cap | **Tyrex** orchestration + config; may include Gamma/CLOB availability. |
| “Instrument not found” on mass reports | Token not in `Cache` yet | Often **noise** after restart or zero-bootstrap until warmup/submit. |
| Position cap under/over | `net_exposure` vs mark price | **Partial** — adapter must feed `Portfolio`; see `phase_a_closure.md`. |

---

## 5. Restart reality

`TradingNodeConfig`: **`load_state=False`, `save_state=False`** (Tyrex). Post-restart: instruments/orders/positions appear as **Nautilus + adapter** reconcile and as **Tyrex** warms the cache (optional). Risk uses **current** reader snapshots — **not** a separate durable Tyrex ledger. See `phase_a_closure.md` § Restart.

**Phase B in production:** B2 is **mark-hungry**: with **default** `fail_on_unresolved_portfolio_exposure`, every **non-flat** Polymarket instrument in `Cache` needs a resolvable mark or B1 stays incomplete → **`RISK_PORTFOLIO_EXPOSURE_UNRESOLVED`** until quotes / `net_exposure` paths are consistent. **`E_portfolio = E_pending + abs(E_filled_net)`** is the **locked** conservative scalar (plan §4.3). Operational checklist and live-session questions: **`phase_b_operational_validation.md`**.

---

## 6. What to read next

| Topic | Doc |
|-------|-----|
| Phase A closure checklist | `phase_a_closure.md` |
| Dynamic / zero-bootstrap | `step_5_runtime_integration.md` |
| Framework submit | `step_4_runtime_integration.md` |
| Reader introduction | `step_3_runtime_integration.md` |
| Strategic plan (unchanged intent) | `road_map.md` (see implementation snapshot section) |
| Operators | `../OPERATIONS.md` |
| Fields | `../CONFIG_MODEL.md` |
| Pre–Phase C operational validation | `phase_b_operational_validation.md` |
| Test coverage vs live gaps (Phase A+B) | `phase_ab_test_validation_matrix.md` |
| Logging workflow (`run_guru.py`, Tyrex vs Nautilus) | `logging_workflow_review.md` |

---

## 7. Phase B closure note

**B5** finalized operator-facing docs (`OPERATIONS.md` matrix, reason codes, startup line). **Stabilization before Phase C** is documented in **`phase_b_operational_validation.md`** (restart, quotes, `E_portfolio` intuition, denial noise). For strategic roadmap text, use `road_map.md` and **`Phase_B_planing.md`** (B0–B5 in §10). **Phase C** is follow-policy / product tuning — not silent changes to §4 exposure semantics.
