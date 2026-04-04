# Phase B — execution plan (Tyrex_PM)

**Status (codebase):** **B0–B5 implemented** — see §10 milestone statuses. This document remains the **normative exposure contract** (§4) and milestone history. **Before Phase C**, run the operational pass in **`phase_b_operational_validation.md`** (restarts, marks, real denial rates).

**Inputs:** `road_map.md` Phase B, `phase_a_closure.md`, `current_state.md`, `OPERATIONS.md` § Phase B.

**Filename note:** `Phase_B_planing.md` is the path requested by maintainers (retain spelling).

**Architecture standards (this revision):**

- **Clean solution:** one aggregation path, one capital source contract, one guru-identity precedence.
- **No quiet fallbacks:** unsupported path + enabled framework-truth gate = **invalid config** or **fail-closed**; never **silent skip**, **silent no-op**, or **log-only degradation** when an operator enables a real-state gate.
- **Maximal framework-backed truth:** Phase B portfolio/concurrency gates are **framework-submit + Nautilus live** only; **`Cache` / `Portfolio`** are the measurement surfaces.
- **Explicit unsupported-path behavior:** **Py-clob-only live** and **shadow** cannot truthfully run Phase B gates that require `Cache`-visible guru orders—**validation rejects** those combinations when gates are on.
- **Precise exposure semantics:** **§4 (Exposure Semantics Contract)** is **normative** before B1–B4 implementation.

---

## 1. Purpose

Define **what Phase B means in this repository**, what Phase A already covers, what remains, and **incremental milestones** with **strict** acceptance criteria—without implementing here and **without Phase C** follow-policy expansion.

---

## 2. Current state after Phase A (relevant to Phase B)

| Capability | State | Evidence |
|------------|--------|----------|
| Framework guru submit | **Complete** (opt-in) | `NautilusGuruExecutionPort`, `polymarket_framework_submit` |
| Pending from open orders | **Complete** on framework path | `_pending_open_notional_from_cache` — **leaves × limit price** |
| Filled exposure per token | **Partial** (adapter-dependent timeliness) | `NautilusPositionStateReader` + `Portfolio.net_exposure` |
| Per-token cap (filled + pending + new) | **Complete** | `max_token_notional_usd_open`, framework path |
| Capital gate (account present + allowance mins, TTL) | **Complete** (opt-in) | `capital_gate_enabled`, `ClobAllowanceStateProvider` |
| Portfolio-wide / concurrency / reserve (B2–B4) | **Implemented** | `portfolio_exposure.py`, `configured.py`, `reason_codes`, B5 docs/log |

---

## 3. What Phase B means in this repo (roadmap vs code)

### 3.1 Roadmap intent

Phase B evolves risk toward **pending**, **position**, and **capital** rules **grounded in measured state**, plus **portfolio** and **concurrency** limits—not **session `_token_open`** for the framework path.

### 3.2 Already satisfied (Phase A) — not repeated in Phase B milestones

- Per-token **filled + pending + new** vs `max_token_notional_usd_open` on **framework path**.
- **Leaves-based** pending; **capital_gate** with **py-clob** balance/allowance **floor** checks and TTL refresh.

### 3.3 Phase B delivers (this document)

- **Portfolio-wide** cap using **§4** exposure definition (framework-only).
- **Concurrent guru resting orders** cap (framework-only, **§5** identity).
- **Reserve / free-after-reserve** on **one canonical collateral source** (**§6**), extending—not replacing—Phase A capital checks where applicable.
- **Startup/config validation** so **no operator can enable Phase B real-state gates on an unsupported path without an **explicit error**** (**§7**).

### 3.4 Deferred to Phase C

Cooldown ladders, burst prioritization, repeated-buy rules, per-cycle follow throttles, pending suppression **as policy**, venue normalization product modes, guru ranking.

---

## 4. Exposure Semantics Contract (normative for Phase B)

This section **must** be implemented **as specified**; milestones B1–B3 **depend** on it.

### 4.1 Scope

| Dimension | Definition |
|-----------|------------|
| **Venue** | **`POLYMARKET` only** (`POLYMARKET_VENUE` / adapter venue constant). |
| **Node** | **This `TradingNode`’s** `Cache` and `Portfolio` only—not multi-process, not cross-trader, not other venues. |
| **Generality** | Phase B is **not** a generic multi-venue portfolio risk engine; it is **Tyrex guru node, Polymarket outcome instruments**. |

### 4.2 Pending exposure

| Item | Semantics |
|------|------------|
| **Source** | `Cache.orders_open` filtered to orders whose `instrument_id.venue == POLYMARKET`. |
| **Quantity** | **Remaining working quantity** — Nautilus **`leaves_qty`** (Tyrex **`OrderSnapshot.leaves_quantity`**). |
| **Valuation** | **Limit price** on each resting limit order (**`OrderSnapshot.price`**). |
| **Per-order notional** | `leaves_quantity × price` (float conversion; invalid parse → **unresolved** for that order). |
| **Portfolio pending total** | **Sum** of per-order notionals across **all** such open orders (all sides). **Gross resting notional** — no netting between opposing orders on the same token in the pending leg (Tyrex does not infer offsetting rests without explicit netting rules). |

### 4.3 Filled exposure

| Item | Semantics |
|------|------------|
| **Source** | `Portfolio.net_exposure(instrument_id, price=mark)` for each **`InstrumentId`** in **`Cache.instruments(venue=POLYMARKET)`** that has a **non-flat** position **or** is omitted if **flat** (zero exposure contribution). |
| **Interpretation** | Use **Nautilus’s** `net_exposure` **Money** scalar in **account cost currency** (USDC for Polymarket outcomes in current modeling). **Signed** net exposure is the **authoritative** filled leg per instrument. |
| **Per-instrument filled contribution** | At mark \(i\): **signed** `float(net_exposure(instrument_id_i, price=mark_i))`. Instruments with **flat** position contribute **0** and may be omitted from the sum. |
| **`E_filled_net` (book-level)** | **Single scalar:** \(\displaystyle E_{\text{filled\_net}} = \sum_i \text{signed net exposure at } \text{mark}_i\). This is the **framework-backed net book** in USDC nominal space (offsetting long/short across instruments nets inside this sum). |
| **`E_portfolio` (cap comparator, locked)** | **`E_portfolio = E_pending + abs(E_filled_net)`** where **`E_pending`** is **§4.2** gross resting notional (**≥ 0**). Taking **`abs`** on the **filled** bucket yields a **conservative ceiling** on **scale of net filled risk** without claiming a separate short-policy in Phase B v1. **Not** multi-venue VAR; **not** a generic gross-of-instruments sum unless promoted by a later ADR. |

**Why not `E_pending + max(E_filled_net, 0)` for v1:** That **long-only** book scalar **understates** magnitude when **`E_filled_net < 0`** (net short). Phase B locks **`abs(E_filled_net)`** so the cap remains **symmetric in book direction** at the **scalar** level while still using **signed** `net_exposure` **per instrument** inside the sum.

**Optional later (Phase C / ADR, not v1 default):** alternate scalars (e.g. long-leg-only, or gross sum of per-instrument **abs** filled exposure) require explicit product spec — **out of scope** here.

### 4.4 BUY vs SELL (intent and resting orders)

- **Resting orders:** Both **BUY** and **SELL** rests contribute **additively** to **`E_pending`** via leaves × price (**nonnegative** per order).
- **Intent notional `n`:** `price_ref × quantity` for the **incoming** intent; applied **symmetrically** for cap math **addition** to **`E_portfolio`** for the **deny** check (**§4.5**).
- **SELL intent and filled leg:** `net_exposure` already reflects position side; no separate asymmetric rule in Phase B v1 beyond **`n`** entering the same inequality.

### 4.5 Current intent inclusion

Deny if:

\[
E_{\text{portfolio}} + n > C
\]

where **`E_portfolio`** is computed from **state before** submitting the intent, **`n`** is the **current intent** notional (from `price_ref` × `qty`), and **`C`** is `max_portfolio_notional_usd_open`. **No** double-count: the new order is **not** in `Cache.orders_open` yet when `evaluate` runs.

### 4.6 Mark source precedence (canonical)

| Priority | Source | Applies to |
|----------|--------|------------|
| **1** | **`intent.price_ref`** | **Only** the **instrument** resolved from **`intent.token_id`** for the **current** `evaluate` call (for recomputing that instrument’s contribution to **`E_filled_net`** consistently with **other** instruments, **or** for a dedicated single-token adjustment—**implementation** may re-value only instruments lacking cache marks; **minimum bar:** **intent instrument** always uses **`intent.price_ref`** when computing **that** term’s `net_exposure` in the portfolio sum **if** it appears in the portfolio aggregation loop). |
| **2** | **Nautilus `Cache` last/mid/trade price** for `InstrumentId` if the adapter exposes a **documented** price getter for **`BinaryOption`** (exact API **version-pinned** in implementation notes). |
| **3** | **None** — mark **unresolved**. |

**Default on unresolved mark (any instrument required for the portfolio sum):**

- **`fail_on_unresolved_portfolio_exposure`** (new `RiskSettings`, **default `true`**): **`evaluate` → fail-closed** with **`ReasonCode.RISK_PORTFOLIO_EXPOSURE_UNRESOLVED`** (or similar stable code).
- If operator sets **`false`**: **explicit opt-in only**; **documented as unsafe** for strict cap correctness—implementation **must** log **once per deny path** that **underestimation is possible**; **not** the default.

**No** silent omission of an instrument from the sum.

### 4.7 Partial fills

- **Pending:** `leaves` already reflects partials (**Phase A**).
- **Filled:** `net_exposure` reflects position after adapter updates.
- **Composition:** **consistent** iff **Nautilus** keeps `Cache`/`Portfolio` consistent; Tyrex does not reconcile beyond framework state.

### 4.8 Legacy / py-clob guru path

**`Cache` does not contain** guru orders submitted via **`PolymarketExecutionPolicy`**. Therefore:

- **Portfolio-wide** and **concurrent guru order** Phase B gates are **undefined** on that path.
- **Remediation:** **§7 validation** — not “soft skip.”

---

## 5. Guru resting-order identity (concurrency cap)

**Preference order** (implementers **must** try in sequence; **document** which is active after B3):

1. **Nautilus `Order` tags** — If `Order.tags` (or equivalent) is **readable** on cached orders and Tyrex **`NautilusGuruExecutionPort`** attaches a **stable** tag (e.g. `guru_cid=...`), extend **`OrderSnapshot`** to carry **`tags`** and match **`guru_cid=`** prefix or **canonical tag** `TYREX_GURU=1` if introduced.
2. **Tyrex-owned explicit flag in reader** — If tags are unreliable, **`NautilusExecutionStateReader`** (or helper) exposes **`is_guru_resting_order(snap: OrderSnapshot) -> bool`** encapsulating **multiple** signals behind one API so **risk** does not scatter heuristics.
3. **`ClientOrderId` prefix `TX`** — **Fallback only** if (1)–(2) are **not available or not reliable** for the pinned Nautilus version. **Document** as **contract tied to** `_client_order_id_from_guru_correlation` in `nautilus_guru_exec.py`; **any change** to ID generation **must** update the helper.

**Multi-strategy:** Tyrex **guru node** is **single-strategy** today; if multiple strategies share a node in future, **(1)** tags are **mandatory** — **blocked** until then (document in B3 acceptance).

---

## 6. Canonical capital source (reserve / free-after-reserve)

| Role | Canonical source | Rationale |
|------|------------------|-----------|
| **Collateral balance for reserve math** | **py-clob `get_balance_allowance`** (**`AssetType.COLLATERAL`**) **`balance`** field, via existing **`ClobAllowanceStateProvider.snapshot()`** | Same **venue-truth** path operators verify with `verify_polymarket_auth`; **not** derived from **`Portfolio.account(venue)` to_dict`**, which is **not** the documented USDC collateral contract for Phase B v1. |
| **Staleness** | **Reuse** Phase A **`max_allowance_snapshot_age_seconds`** refresh policy for the **same snapshot** used for mins and reserve (single refresh loop per `evaluate` when gate enabled). |
| **Allowance (`allowance` field)** | **Separate** Phase A **`min_allowance_usd`** gate — **unchanged**; reserve check runs **after** allowance **floor** passes (or **ordering**: **account_present** → **allowance mins** → **reserve** — **exact order** in implementation doc). |
| **Unavailable (`allowance_provider` None or parse fails)** | **Fail-closed** when **`collateral_reserve_usd > 0`** or reserve gate enabled — **same** as Phase A capital gate: **no** silent bypass. |

**Shadow mode:** **`ClobAllowanceStateProvider` is None** — reserve gate **cannot** run—**§7** makes **reserve > 0** **invalid** in shadow.

---

## 7. Startup and config validation (no silent no-op)

**Principle:** Enabling a Phase B **framework-truth** feature on a path **without** framework truth is **`ValueError` at load or compose** — **not** runtime warning and **not** skip.

### 7.1 “Framework Phase B eligible” predicate

**True** iff:

- `execution_mode == live`
- `polymarket_nautilus_live == true`
- `polymarket_framework_submit == true`

### 7.2 Gates requiring framework truth

Any **non-default** activation of:

- `max_portfolio_notional_usd_open` **finite** (< ∞),
- `max_concurrent_guru_resting_orders` **non-null** positive int,
- (optional) **`fail_on_unresolved_portfolio_exposure`** only meaningful with portfolio cap — no extra flag needed for validation

### 7.3 Validation rules

| Condition | Result |
|-----------|--------|
| Any **§7.2** gate active **and** **not §7.1** | **`ValueError`** from `load_risk_settings` **after** merging runtime context **or** from **`build_guru_trading_node`** with message: *Phase B gate X requires live + polymarket_nautilus_live + polymarket_framework_submit*. |
| `execution_mode == shadow` **and** **`collateral_reserve_usd > 0`** | **`ValueError`** (no py-clob provider). |
| `execution_mode == shadow` **and** **§7.2** gate active | **`ValueError`**. |
| **§7.2** inactive (all defaults) | No Phase B framework requirement. |

### 7.4 Phase A `capital_gate_enabled` interaction

**Independent** unless reserve is implemented inside the same policy: **reserve** requires **`capital_gate_enabled == true`** **or** reserve triggers **its own** allowance snapshot fetch—**implementation choice** in B4: **mandate** `capital_gate_enabled` when `collateral_reserve_usd > 0` **to avoid duplicate HTTP** — **recommended:** **`collateral_reserve_usd > 0` ⇒ `capital_gate_enabled` required** — **validate** at load.

---

## 8. Readiness assessment (updated)

| Area | Ready | Partial | Blocked |
|------|--------|---------|---------|
| Pending aggregation | **Yes** (framework) | — | Legacy **invalid** if Phase B gates on |
| Filled aggregation | **Yes** via `net_exposure` | Adapter **stale** state | — |
| Marks | **Intent** always | **Cache** quote **version-dependent** | Missing quote ⇒ **fail-closed** default |
| Guru ID | **Yes** (B3: tags `guru_cid=` then `TX`+26 hex fallback) | — | — |
| Capital reserve | **Yes** (B4 in ``ConfiguredRiskPolicy``) | — | — |

---

## 9. Proposed Phase B scope (crisp)

### In scope

- **B0:** Lock **§4–§7** in code comments + **loader/compose validation**.
- **B1:** Portfolio exposure aggregator (**§4**).
- **B2:** Portfolio-wide cap + **`fail_on_unresolved_portfolio_exposure`** (default **true**).
- **B3:** Concurrent **guru** resting orders (**§5**).
- **B4:** Reserve / free-after-reserve (**§6**) + validation with **`capital_gate_enabled`**.
- **B5:** Docs operator matrix + reason codes + **`CONFIG_MODEL`**.

### Out of scope

Phase C items; **multi-venue** caps; **heuristic** exposure when framework state missing.

---

## 10. Implementation milestones

### Milestone B0 — Exposure semantics + validation contract (code + docs)

**Objective:** Implement **§4–§7** as **comments**, **`RiskSettings` placeholders** if needed, **`load_risk_settings` / `build_guru_trading_node` validation** for **unsupported combinations** — **before** aggregation logic ships.

**Files:** `config/loaders.py`, `runtime/guru_compose.py`, `risk/configured.py` (stubs OK), `Docs/CONFIG_MODEL.md`, this file cross-checked.

**Acceptance:** **Unit tests** that **invalid** YAML combos **raise**; **valid** framework triple passes.

**Operator impact:** **Hard failure** at startup if misconfigured — **explicit errors**.

**Implementation status (codebase):** **B0 complete** — `RiskSettings` carries Phase B fields; `load_risk_settings` enforces reserve vs `capital_gate_enabled`; `validate_phase_b_runtime_contract` runs at the start of `build_guru_trading_node`; tests in `tests/test_phase_b_b0_validation.py`. Shadow + reserve and unsupported framework gates fail at **compose**, not silently at runtime.

---

### Milestone B1 — Runtime: portfolio exposure aggregation

**Objective:** `NautilusPortfolioExposureAggregator` (or equivalent) implementing **§4** exactly; **`E_portfolio`** and **`E_pending`**, **`E_filled_net`** exposed for tests.

**Files:** `runtime/portfolio_exposure.py` (preferred) or `state_readers.py`, tests.

**Dependencies:** **B0** merged.

**Acceptance:** Golden mocks for multi-order, multi-instrument; **unresolved mark** with default flag **denies** in caller dry-run tests.

**Implementation status (codebase):** **B1 complete** — ``NautilusPortfolioExposureAggregator`` in ``src/tyrex_pm/runtime/portfolio_exposure.py`` computes ``E_pending``, ``E_filled_net``, ``E_portfolio`` per §4; marks §4.6 (``intent.price_ref`` then ``Cache`` LAST/MID/MARK + ``mark_price``); ``PortfolioExposureAggregate`` carries ``complete`` / ``error`` / ``omitted_instruments_unresolved_mark``; wired on ``GuruTradingAssembly.portfolio_exposure`` when live + framework submit. Tests: ``tests/test_portfolio_exposure.py``.

---

### Milestone B2 — Risk: portfolio-wide notional cap

**Objective:** `max_portfolio_notional_usd_open`; deny when **`E_portfolio + n > C`**; **`ReasonCode`** for breach and unresolved.

**Files:** `risk/configured.py`, `reason_codes.py`; use ``GuruTradingAssembly.portfolio_exposure`` (or inject into ``ConfiguredRiskPolicy``) in ``evaluate`` — **aggregator already constructed in B1**.

**Dependencies:** **B1**, **B0** validation.

**Acceptance:** **Framework-only** path tested; **legacy** path with cap enabled **never** constructed (validation test).

**Implementation status (codebase):** **B2 complete** — ``ConfiguredRiskPolicy`` injects ``NautilusPortfolioExposureAggregator``; ``_portfolio_wide_cap_eval`` denies ``RISK_PORTFOLIO_EXPOSURE_UNRESOLVED`` on **any** incomplete aggregate or missing ``e_portfolio`` (including when ``fail_on_unresolved_portfolio_exposure`` is false). Unsafe mode only adds underestimation **warning + cap** when the aggregate is **complete** with ``omitted_instruments_unresolved_mark``. ``guru_compose`` passes ``portfolio_agg``. Tests: ``tests/test_phase_b_b2_portfolio_cap.py``.

---

### Milestone B3 — Risk: concurrent guru resting-order cap

**Objective:** **`max_concurrent_guru_resting_orders`**; count orders where **`is_guru_resting_order`** per **§5** preference order implemented.

**Files:** `state_readers.py` / `nautilus_guru_exec` tag contract, `risk/configured.py`, tests.

**Dependencies:** **B0** validation.

**Acceptance:** Tag-based test if available; fallback **TX** test with **explicit** comment in code that it is **fallback tier 3**.

**Implementation status (codebase):** **B3 complete** — ``is_guru_resting_order`` + ``NautilusExecutionStateReader.count_guru_resting_orders_open`` in ``state_readers.py`` (Polymarket venue); ``ConfiguredRiskPolicy._guru_concurrent_resting_cap_eval`` denies when ``count >= limit`` with ``RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT``. **Active identity:** tier **1** ``guru_cid=`` tags on snapshots when present; tier **3** ``TX``+26 hex (``nautilus_guru_exec``) if tags empty. Tests: ``tests/test_phase_b_b3_concurrent_guru_orders.py``.

---

### Milestone B4 — Risk: reserve / free-after-reserve

**Objective:** **`collateral_reserve_usd`**; BUY: require **`balance ≥ reserve + n`** (after Phase A mins ordering **per §6**); **`capital_gate_enabled` required** when reserve > 0.

**Files:** `risk/configured.py`, `loaders.py`, validation, `reason_codes.py`.

**Dependencies:** **B0**, Phase A capital path.

**Acceptance:** Fail-closed when snapshot missing; **shadow** + reserve **invalid** at load.

**Implementation status (codebase):** **B4 complete** — ``ConfiguredRiskPolicy._capital_gate_eval`` extends the Phase A py-clob snapshot fetch when ``collateral_reserve_usd > 0`` (single cache + ``max_allowance_snapshot_age_seconds``). Ordering: account_present → ``min_collateral_balance_usd`` / ``min_allowance_usd`` → reserve. **BUY** only: deny when ``balance < reserve + n`` with ``RISK_INSUFFICIENT_FREE_COLLATERAL_AFTER_RESERVE``; missing ``n`` aligns with B2 (``RISK_MISSING_PRICE``). Tests: ``tests/test_phase_b_b4_reserve.py``.

---

### Milestone B5 — Integration: docs, operator matrix, logs

**Objective:** **`OPERATIONS.md`**, **`current_state.md`**, **`phase_a_closure.md`** cross-links; startup log line listing **active Phase B gates** (optional, **non-silent** list).

**Dependencies:** B2–B4 complete.

**Implementation status (codebase):** **B5 complete** — **`Docs/OPERATIONS.md`** § Phase B: runtime **matrix** (shadow / legacy live / framework triple) for B2–B4; **no-op / inert settings** called out; **Phase B reason code** operator table; Phase C defer pointer. **`current_state.md`**, **`phase_a_closure.md`**, **`CONFIG_MODEL.md`**, module **`risk` / `runtime` READMEs**, **`run_guru.py`** (``tyrex_pm`` logger INFO so compose line is visible). **Startup:** :func:`tyrex_pm.runtime.phase_b_startup.phase_b_startup_summary_line` + INFO log from :func:`tyrex_pm.runtime.guru_compose.build_guru_trading_node`; tests: ``tests/test_phase_b_b5_startup_log.py``.

---

## 11. Acceptance criteria by milestone (summary)

| ID | Criteria |
|----|-----------|
| **B0** | Unsupported path + enabled gate ⇒ **`ValueError`**; docs cite **§4–§7**; shadow + reserve ⇒ **invalid**. |
| **B1** | **`E_portfolio`** matches **§4** on mocks; no silent drop; unresolved mark ⇒ aggregator returns **error / sentinel** consumed by B2 as deny if default flag. |
| **B2** | Deny **over cap**; **fail-closed** unresolved with default flag; **no** legacy silent path. |
| **B3** | Deny at **≥ limit**; guru ID tier **documented in code**. |
| **B4** | Reserve math on **py-clob balance only**; **fail-closed** on missing; **invalid** without `capital_gate_enabled` when reserve > 0. |
| **B5** | Operator matrix **complete**; **no** undocumented no-ops. |

---

## 12. Risks / upstream dependencies (remaining)

1. **Nautilus `Cache` quote access** for **`BinaryOption`** — if **no** API for non-intent instruments, **all** portfolio caps may **fail-closed** until guru warms prices — **blocked** on adapter surface; **document** in B1.
2. **`net_exposure` None** — **fail-closed** by default per **§4.6**.
3. **Adapter staleness** — operational, not Tyrex-fixable; **document**.
4. **Tags not exposed on `Order` in installed version** — **fall back to tier 3** with **explicit** tech-debt note in B3.

---

## 13. What is deferred to Phase C

Same as prior plan: cooldown/burst/follow **policy**, venue normalize, guru ranking, **alternate §4.3 cap comparators** (e.g. long-only filled leg vs today’s `abs(E_filled_net)`) unless promoted earlier by explicit ADR.

**Operational tuning** (whether default strict B2 is too noisy in your deployment) is **not** a code change in Phase B — validate per **`phase_b_operational_validation.md`**, then drive Phase C / ADR from evidence.

---

## 14. Recommended execution order

**B0 → B1 → B2 → B3 → B4 → B5**

**B3** may parallelize **after B0** with **B1** for **reader** work only—not **merge** with B2 in one PR without review.

---

## Document history

- **2026-04:** Initial plan.
- **2026-04 (revision):** **Exposure Semantics Contract**; **B0** validation; **no quiet fallbacks**; **framework-only** Phase B gates; **canonical marks** and **fail-closed** default; **guru ID** precedence; **py-clob balance** canonical for reserve; **`E_portfolio = E_pending + abs(E_filled_net)`** locked.
- **2026-04 (B0 code):** `tyrex_pm.config.loaders` — Phase B `RiskSettings` fields, `validate_phase_b_runtime_contract`, reserve vs capital gate at load; `guru_compose` calls validation before node build; `tests/test_phase_b_b0_validation.py`.
- **2026-04 (B1 code):** `tyrex_pm.runtime.portfolio_exposure` — `NautilusPortfolioExposureAggregator`, `GuruTradingAssembly.portfolio_exposure`; `tests/test_portfolio_exposure.py`.
- **2026-04 (B2 code):** `ConfiguredRiskPolicy` portfolio-wide gate + `ReasonCode` `RISK_PORTFOLIO_*`; `tests/test_phase_b_b2_portfolio_cap.py`.
- **2026-04 (B3 code):** guru resting-order cap, `is_guru_resting_order`, `OrderSnapshot.tags`; `tests/test_phase_b_b3_concurrent_guru_orders.py`.
- **2026-04 (B4 code):** reserve / free-after-reserve in `ConfiguredRiskPolicy._capital_gate_eval`; `tests/test_phase_b_b4_reserve.py`.
- **2026-04 (B5 docs/code):** operator matrix + reason tables in `OPERATIONS.md`; `phase_b_startup.py` + compose INFO log; `tests/test_phase_b_b5_startup_log.py`.
