# C2 validation readiness review — Capital Allocation

Connects **business objective → architecture → code → tests → telemetry → operator plan**. Based on inspection of the Tyrex_PM codebase as implemented for the C2 MVP (conviction sizing + min-follow-notional gate).

---

## 1. Business objective

C2 improves **how scarce follower capital is allocated** relative to a flat **`copy_scale`**:

- **Conviction-weighted sizing** scales follow size up or down based on the guru’s current **BUY** leg size vs a **rolling average** of recent **accepted entry** sizes (bounded by a cap).
- **Minimum-follow-notional** skips intents whose estimated **`price_ref × qty`** is below an operator floor, before risk — avoiding “dust” follows that burn attention and downstream evaluation.

C2 **does not** change **Phase B risk** semantics, **execution** mechanics, or **`GuruTradeSignal`**.

---

## 2. Architecture mapping

| Concern | Layer | Implementation |
|--------|--------|----------------|
| **How much?** (conviction) | Policy / `signal/` | `ConvictionProportionalSizingPolicy` / `ProportionalSizingPolicy` in `src/tyrex_pm/signal/sizing.py`; composed via `build_sizing_policy`. |
| **Rolling context** | In-memory inside sizing policy | `deque` in `ConvictionProportionalSizingPolicy` (`sizing.py`) — **no persistence** across process restart; **only** sizes recorded via `record_accepted_entry_size` (positive `size_raw`). |
| **Worth doing?** (economic floor) | Policy / `signal/` | `FollowWorthinessGate` in `src/tyrex_pm/signal/follow_worthiness.py`. |
| **Orchestration order** | Strategy | `CopyStrategy._handle_branch` (`copy_strategy.py`): entry/exit `evaluate` → `size(..., branch=kind)` → **`record_accepted_entry_size` only if `kind == "entry"`** → zero-qty check → **`_worthiness.evaluate(price_ref, qty)`** → optional DEBUG `copy_conviction_diag` → `OrderIntent` → **`_risk.evaluate`** → execution port. |
| **Safe / allowed?** | Risk (unchanged) | `RiskPolicy` / `ConfiguredRiskPolicy` — **not** modified for C2; min-notional is **not** delegated to risk when enabled. |
| **Venue expression** | Execution (unchanged) | `ExecutionPort` / Nautilus paths — **no C2 changes**. |
| **Config injection** | Loaders + compose | `StrategySettings` fields in `load_strategy_settings` (`loaders.py`); `CopyStrategyConfig(...)` in `guru_compose.py` `build_guru_trading_node`. |
| **Telemetry** | Strategy + enum | `copy_skip` with C2 `reason_code` strings; `ReasonCode.MIN_FOLLOW_NOTIONAL`, `MIN_FOLLOW_NOTIONAL_PRICE_MISSING` in `core/reason_codes.py`; DEBUG `copy_conviction_diag` for accepted entries when conviction is on. |

**Split (normative):**

- **Policy / signal:** “How much?” and “Worth copying at this notional?” → sizing + worthiness gate.
- **Risk:** “Safe and within limits?” → unchanged **`risk.evaluate(intent)`** after `OrderIntent` exists.
- **Execution:** unchanged in C2.

---

## 3. Code review summary

**Files / symbols**

- `signal/sizing.py` — `SizingPolicy` (Protocol), `ProportionalSizingPolicy`, `ConvictionProportionalSizingPolicy` (cold start: empty buffer ⇒ ratio `min(1, cap)`; entry uses `effective_scale = base_scale * min(trade_size/avg, cap)`; exit uses `base_scale` only), `build_sizing_policy`.
- `signal/follow_worthiness.py` — `FollowWorthinessGate.evaluate(price_ref, qty)`; threshold `<= 0` passes; else missing price ⇒ `min_follow_notional_price_missing`; below floor ⇒ `min_follow_notional`.
- `strategy/copy_strategy.py` — `CopyStrategyConfig` C2 fields; wiring above; INFO `copy_skip` on worthiness deny with `base_scale`, `effective_scale`, `guru_size_raw`, `rolling_avg_guru_size`, `estimated_notional_usd`.
- `config/loaders.py` — `StrategySettings.conviction_*`, `min_follow_notional_usd`; validation when conviction enabled (`lookback >= 1`, `cap > 0`); `min_follow >= 0`.
- `runtime/guru_compose.py` — passes strategy fields into `CopyStrategyConfig`.
- `core/reason_codes.py` — C2 reason value strings.

**Plan alignment**

- Matches locked **Part 1** decisions in `plan_C2_Capital-Allocation.md`: BUY accepted-entry-only average observation (enforced by **only** calling `record_accepted_entry_size` after accepted entry path); missing price + floor ⇒ policy skip; split sizing vs worthiness; default-off preserves proportional behavior via `build_sizing_policy` → `ProportionalSizingPolicy`.

**Architectural drift**

- None material: worthiness stays out of `ConfiguredRiskPolicy`; sizing stays out of execution.

**Risk / misplaced logic**

- **In-memory rolling window** resets on restart — documented in plan; operators should expect short **cold-start** behavior after boot (ratio effectively neutralized toward `min(1,cap)` until buffer fills).
- **`shadow_order_intent` / `live_order_intent`** log **qty** but not **effective_scale** (see §5) — baseline vs C2 comparisons rely on **qty**, **DEBUG** diagnostics, or **worthiness** skip lines.

**Default-off**

- `conviction_sizing_enabled` defaults **false** in YAML loader; `build_sizing_policy` returns **`ProportionalSizingPolicy`** — same formula as pre-C2 `max(0, size_raw * copy_scale)` with branch-aware metrics only.

---

## 4. Specific tests and what each proves

Inventory from `tests/unit/test_c2_capital_allocation.py` and `tests/unit/test_copy_strategy_shadow.py`. *Full suite:* `pytest` includes these; count varies with repo.

| File | Test name | Proves | Type | Edge / notes |
|------|-----------|--------|------|----------------|
| `test_c2_capital_allocation.py` | `test_proportional_sizing_matches_pre_c2_with_branch` | Proportional policy ignores branch for math; `0.5 * 10` | Unit | Branch param present |
| `test_c2_capital_allocation.py` | `test_build_sizing_policy_disabled_is_proportional` | **Flag off** → `ProportionalSizingPolicy` instance + same qty | Unit + wiring | **Regression guard** |
| `test_c2_capital_allocation.py` | `test_conviction_cold_start_ratio_one` | Empty buffer → qty = guru size × base; `rolling_avg_guru_size` None; `effective_scale` == base | Unit | Cold start |
| `test_c2_capital_allocation.py` | `test_conviction_second_trade_uses_avg` | After one `record_accepted_entry_size(10)`, trade 20 → ratio 2, qty 40 | Unit | Rolling avg math |
| `test_c2_capital_allocation.py` | `test_conviction_cap_binds` | Raw ratio 3, cap 1.2 → eff 1.2, qty 36 | Unit | **Cap binding** |
| `test_c2_capital_allocation.py` | `test_conviction_exit_uses_base_only` | `branch="exit"` ignores conviction; `0.5 * 100` | Unit | **SELL / exit path** |
| `test_c2_capital_allocation.py` | `test_conviction_buffer_only_records_positive_raw` | Zero / null size does not pollute buffer | Unit | Positive-size filter |
| `test_c2_capital_allocation.py` | `test_follow_worthiness_*` | Gate off; missing price; below min | Unit | **Min-notional + missing price** |
| `test_c2_capital_allocation.py` | `test_load_strategy_c2_defaults` | Defaults: conviction off, min_follow 0 | **Config** | Default-off |
| `test_c2_capital_allocation.py` | `test_load_strategy_c2_conviction_validation` | Enabled + bad lookback → `ValueError` | Config | Validation |
| `test_c2_capital_allocation.py` | `test_load_strategy_rejects_negative_min_follow` | Negative min floor rejected | Config | |
| `test_copy_strategy_shadow.py` | `test_min_follow_notional_skips_small_intent` | End-to-end: small notional → no port record | **Flow** | Min floor |
| `test_copy_strategy_shadow.py` | `test_min_follow_notional_missing_price_skips_when_enabled` | Missing `price_ref` → no intent | **Flow** | **Locked missing-price rule** |
| `test_copy_strategy_shadow.py` | `test_conviction_rejected_buy_does_not_seed_rolling_buffer` | Not-allowlisted BUY then allowlisted BUY — second trade **cold-starts** (qty 20 not 40) | **Flow** | **BUY-only accepted-entry observation** |
| `test_copy_strategy_shadow.py` | `test_conviction_sizing_second_buy_larger_qty_than_flat` | Two accepted BUYs; second qty > first | **Flow** | Conviction on path |
| `test_copy_strategy_shadow.py` | (existing) `test_shadow_emits_intent_*`, etc. | Unchanged strategy behavior when C2 defaults | Flow | **No regression** baseline path |

**Explicitly not separately tested (acceptable / low risk):**

- **Rolling window eviction** at maxlen — implied by `deque(maxlen=...)`; no dedicated test for K>2 trim (optional future test).
- **`c2_shadow_compare`** — deferred by design.

---

## 5. Evidence from logs / telemetry

**C2-specific today**

| Mechanism | Content | Sufficiency |
|-----------|---------|-------------|
| `event=copy_skip` + `reason_code=min_follow_notional` | INFO; includes `base_scale`, `effective_scale`, `guru_size_raw`, `rolling_avg_guru_size`, `estimated_notional_usd` | **Yes** for floor validation |
| `event=copy_skip` + `reason_code=min_follow_notional_price_missing` | Same field pattern; `estimated_notional_usd` typically None | **Yes** for missing-price policy |
| `event=copy_conviction_diag` | DEBUG only; `conviction_ratio`, scales, `qty` | **Yes** if log level ≥ DEBUG for conviction tuning |
| `event=shadow_order_intent` / `live_order_intent` | `qty`, `signal_kind`, latencies | **Partial:** compare **qty** vs baseline run; **no** `effective_scale` on this line |

**Gap (minor, not blocking)**

- Accepted intents do not emit **INFO** conviction fields — operators enable **DEBUG** or infer from **qty** vs guru `size_raw` and `copy_scale`.

**Helper script**

- **Not required** for first validation: `grep`/reports on `copy_skip` + `shadow_order_intent` suffice. A small **`c2_capital_report.py`** (histogram of skip reasons, avg qty ratio) would be **nice-to-have**, not MVP.

---

## 6. Validation scenarios to run

Use **`execution_mode: shadow`** until behavior is trusted; then **narrow live canary** with tight risk caps.

### Scenario A — Baseline (C2 off)

- **Config:** `conviction_sizing_enabled: false`, `min_follow_notional_usd: 0` (defaults in `config/strategy/guru_follow.yaml`).
- **Watch:** `guru_signal_emitted`, `shadow_order_intent` **qty** vs guru `size_raw` × `copy_scale`.
- **Good:** Matches historical behavior; no new `copy_skip` reasons.
- **Bad:** Unexpected qty change — investigate compose/config drift.

### Scenario B — Conviction only

- **Config:** `conviction_sizing_enabled: true`, tune `conviction_sizing_cap`, `conviction_sizing_lookback_trades`; `min_follow_notional_usd: 0`.
- **Watch:** DEBUG `copy_conviction_diag`; compare **`shadow_order_intent` qty** across trades vs Scenario A.
- **Good:** Larger guru legs vs recent average → larger follower qty (capped); exits mirror flat scale; cold-start after restart shows neutral sizing until buffer fills.
- **Bad:** Qty stuck flat → conviction not on or DEBUG missing; absurd qty → cap / scale mis-set.

### Scenario C — Min-notional only

- **Config:** `conviction_sizing_enabled: false`, `min_follow_notional_usd: <operator floor>`.
- **Watch:** `copy_skip` with `min_follow_notional` or `min_follow_notional_price_missing`; **no** `shadow_order_intent` for skipped dust.
- **Good:** Skips align with `price_ref * qty` estimate; missing price skips when floor > 0.
- **Bad:** Mass `price_missing` — data path / guru price field issue; tune floor vs venue min separately.

### Scenario D — Combined C2

- **Config:** both features on with conservative cap and moderate floor.
- **Watch:** Both telemetry types; order of operations: worthiness **after** sizing (large conviction qty can still trip floor).
- **Good:** Coherent logs; no risk/execution anomalies.
- **Bad:** Unexpected **risk_denied** spike — likely unrelated to C2; check caps.

### Scenario E — Narrow live canary

- **Only after** A–D in shadow: **live** runtime + smallest practical size, **unchanged** risk YAML limits, C2 settings as intended.
- **Watch:** `live_order_intent`, `LIVE_ORDER_SUBMIT` / errors, `tyrex_risk_ops` if denies; compare to shadow qty pattern.
- **Good:** Submits match shadow intent pattern within risk.
- **Bad:** Duplicate submits / wrong qty — stop; use `scripts/guru_primary_report.py` and risk logs.

**Comparison metric:** **Distribution of follower `qty` / estimated notional** vs **Scenario A** on the same guru stream window (same signals, different allocation policy).

---

## 7. Are small patches needed before validation?

**Before this review’s test addition:** Rolling-buffer **isolation** (rejected BUY must not seed average) was implied by code but **not** covered by a strategy-level test.

**Patch applied (minimal):**

- `tests/unit/test_copy_strategy_shadow.py` — **`test_conviction_rejected_buy_does_not_seed_rolling_buffer`**.

**Otherwise:** No mandatory code changes for validation credibility. Docs already include C2 in `CONFIG_MODEL.md`, `OPERATIONS.md`, `modules/signal/README.md`.

**Verdict:** C2 is **validation-ready** with the above test.

---

## 8. Recommendation

**Recommendation:** **Ready for validation as-is** (after the **single** added test above; run `pytest tests/unit/test_c2_capital_allocation.py tests/unit/test_copy_strategy_shadow.py`).

**Exact next step:** Run **Scenario A** (baseline) and **Scenario B** (conviction only) in **shadow** with `guru_ingest_mode: rtds_primary`, capture `logs/<mode>/run_nautilus.log` / `run_tyrex.log`, grep `copy_skip` / `copy_conviction_diag` / `shadow_order_intent`, then proceed to **Scenario C** and **D** before any live canary.

**Deferred (unchanged):** `c2_shadow_compare`, persisted rolling conviction state, portfolio / Kelly / multi-guru / execution-aware sizing, C3.
