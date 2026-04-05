# Plan: C2 — Capital Allocation

# 1. Objective

**C2** improves how much of the follower’s **scarce capital** is deployed per guru opportunity and **which** opportunities are worth copying at all—using policy-level sizing and a capital-efficiency floor—**without** moving exposure bookkeeping into strategy, **without** changing Phase B risk ownership or semantics, and **without** changing execution mechanics (order types, slippage, book shape). Risk still answers **safe / allowed**; execution still answers **venue expression**; C2 extends **signal policy** so the path from `GuruTradeSignal` → `OrderIntent` is less blunt than uniform `copy_scale`.

---

# 2. Why C2 is next

**C1** (Time-to-Follow) improved **when** Tyrex observes guru trades (RTDS primary + poll fallback). The next bottleneck for a **small wallet** is not detection latency alone: **uniform scaling** (`copy_scale`) treats every guru leg alike, so capital spreads thinly across noise and size extremes. **C2** targets **which** signals get sized **how much** and skips **economically negligible** follows—directly addressing alpha per dollar deployed.

C2 is a better immediate workstream than **C3** (execution quality): it builds on existing **signal** and **strategy** wiring, needs **no** new Nautilus order machinery, and does not require revisiting Phase B **`ConfiguredRiskPolicy`** contracts. Implementation weight stays bounded if MVP is held to **conviction-weighted sizing** + **minimum-follow-notional** only.

---

# 3. Current workflow audit

**Guru trade size enters the pipeline** as `GuruTradeSignal.size_raw` and `price_raw`, populated from Data API / RTDS rows in `src/tyrex_pm/data/guru_parse.py` (`trade_row_to_signal`, `activity_trade_row_to_signal`). C1 does not change this contract (`src/tyrex_pm/core/types.py` — `GuruTradeSignal`).

**Where `copy_scale` is applied today**

- **YAML:** `config/strategy/*.yaml` → `copy_scale` (optional, default `1.0`); validated in `load_strategy_settings` (`src/tyrex_pm/config/loaders.py`, `StrategySettings.copy_scale`).
- **Compose:** `build_guru_trading_node` passes `strategy.copy_scale` into `CopyStrategyConfig` (`src/tyrex_pm/runtime/guru_compose.py`, `CopyStrategyConfig(..., copy_scale=strategy.copy_scale)`).
- **Strategy:** `CopyStrategy.__init__` constructs `ProportionalSizingPolicy(config.copy_scale)` (`src/tyrex_pm/strategy/copy_strategy.py`).
- **Sizing:** `ProportionalSizingPolicy.size(sig)` returns `max(0, (size_raw or 0) * scale)` (`src/tyrex_pm/signal/sizing.py`).

**Uniformity today:** Every accepted entry/exit (after token filter) uses the **same** scalar `copy_scale` and guru `size_raw`. There is **no** per-trade conviction, no rolling guru context, and no follower-level “too small to matter” gate before risk.

**Pipeline order in `CopyStrategy._handle_branch`** (`copy_strategy.py`)

1. `GuruFollowEntryPolicy` / `GuruMirrorExitPolicy.evaluate` → `SignalDecision` (token allowlist, side) — `src/tyrex_pm/signal/entry.py`.
2. **`_sizing.size(sig)`** → `qty`.
3. If `qty <= 0` → `copy_skip` / `ReasonCode.COPY_SKIP` / `detail=zero_qty`.
4. **`OrderIntent`** built with `quantity=qty`, `price_ref=sig.price_raw`.
5. **`RiskPolicy.evaluate(intent)`** — production: `ConfiguredRiskPolicy` (`src/tyrex_pm/risk/configured.py`): `_estimate_notional(intent)` = `price_ref * quantity` when price present; gates include `max_order_quantity`, `max_notional_usd_per_order`, token / portfolio / concurrent / reserve caps.
6. **`ExecutionPort.submit_intent`**; logs `shadow_order_intent` / `live_order_intent` with `qty`, latencies.

**Where a minimum-follow-notional decision fits naturally:** **After** `qty` is known and **before** `OrderIntent` + **before** `risk.evaluate`—same bracket as `zero_qty`, using the **same notional estimate** risk will use (`price_ref * qty`). That keeps C2 as **policy / worth-doing**, not a safety duplicate of `max_notional_usd_per_order` (which is a **ceiling**).

**Relevant config today**

- **Strategy:** `StrategySettings` / YAML: `guru_wallet_address`, `token_filter`, `copy_scale`, optional `strategy_dedup_state_path` (`loaders.py`).
- **Risk:** `max_notional_usd_per_order`, per-token / portfolio caps, etc. — **unchanged** for C2 MVP ownership.
- **Execution env:** `TYREX_MIN_BUY_NOTIONAL_USD` (venue-oriented floor in execution ports) — **orthogonal** to C2 `min_follow_notional_usd` (policy intent); document both to avoid confusion.

**Metrics / logs usable for C2 validation**

- **`copy_skip`** with `reason_code` / `detail` — extend with explicit C2 reasons (see §9).
- **`shadow_order_intent` / `live_order_intent`** — `qty`, `correlation_id`, latency KV (`ts_event_ms`, `signal_to_submit_ms`, …).
- **`guru_signal_emitted`** (C1) — `correlation_id`, `side`, `token_id`, `ts_event_ms`; ties shadow/primary ingest to strategy outcomes.
- **Risk denies:** `copy_skip` + `risk_denied` — unchanged semantics; C2 should **not** fold policy skips into risk.
- **MVP adds:** INFO `copy_skip` lines for worthiness with structured numeric fields; DEBUG optional for per-entry conviction diagnostics (no mandatory `c2_shadow_compare` mode).

---

# 4. C2 target behavior

## 4.1 Conviction-weighted sizing (MVP)

**Default formula**

Let `base_scale` = today’s `copy_scale` (YAML). Let `trade_size = max(ε, guru size_raw in shares)` and `avg = max(ε, rolling mean of last K guru trade sizes)` where **K** = `conviction_sizing_lookback_trades`.

**Locked observation rule (MVP):** The rolling buffer contains **`size_raw`** only from **BUY** signals that **passed the entry path** (token allowlist + `GuruFollowEntryPolicy` accept). **SELL / exit** legs **do not** update the buffer. **Rejected** BUYs (not allowlisted, etc.) **do not** update the buffer. *Rationale:* C2 sizes **followable entry** opportunities; mixing exits would skew “typical guru leg” toward closes; rejections were never follow candidates. (Aligned with `CopyStrategy._handle_branch` only calling sizing after `decision.accept`.)

\[
\text{effective\_scale} = base\_scale \times \min\left(\frac{trade\_size}{avg},\; conviction\_cap\right)
\qquad
qty = \max(0,\; size\_raw \times effective\_scale)
\]

(`size_raw` and `trade_size` are the same guru share count; today’s behavior is `qty = max(0, size_raw × base_scale)` with `effective_scale = base_scale` when conviction is off or ratio is neutral.)

**Why this proxy:** Larger guru legs vs the guru’s **recent typical** leg are a **cheap, observable** signal of relative attention to the trade without external metadata. It does **not** assume guru PnL or “true” edge—only **relative activity size**.

**What it does not assume:** Optimal portfolio weights, stationarity of guru style, or correlation across markets.

**Why it belongs in sizing policy, not risk:** It does not ask “is the **venue/account** safe within limits?” It asks “how **large** should our **intent** be given guru behavior?” — aligned with `signal/` and `ProportionalSizingPolicy` (`Docs/modules/signal/README.md` boundaries).

## 4.2 Minimum-follow-notional filter (MVP)

After computing `qty` and **before** building `OrderIntent` / calling risk:

- Estimate **follow notional** \(n \approx price\_ref \times qty\) (mirror `_estimate_notional` in `configured.py`).
- **Locked rule (MVP):** If `min_follow_notional_usd > 0` and `price_ref` is **missing**, **skip** with a **dedicated policy** `ReasonCode` (not `risk_denied`). Do **not** defer to Phase B risk for this decision—C2 worthiness must be deterministic. When `min_follow_notional_usd == 0`, missing price is irrelevant to this gate (risk may still fail on missing price as today).

**Not** a venue minimum: execution may still enforce `TYREX_MIN_BUY_NOTIONAL_USD` separately.

**Not** a Phase B safety gate: risk already enforces `max_notional_usd_per_order`, portfolio, etc. C2 `min_follow_notional_usd` is a **floor on economic interest**: “don’t spend attention and risk budget on dust-sized follows.”

**Why before risk:** Avoids burning risk evaluation / concurrent-order slots / log noise on intents the operator considers **not worth following**; keeps **`ConfiguredRiskPolicy`** focused on **allow / deny** for serious intents.

---

# 5. Architectural placement

| Concern | Owner (C2 MVP) | Unchanged |
|---------|------------------|-----------|
| Conviction-weighted multiplier / rolling stats | **`signal/sizing.py`** — `SizingPolicy` protocol + `ConvictionProportionalSizingPolicy` (rolling deque, BUY-accepted-only observation rule §4.1) | **`GuruTradeSignal`** schema unchanged; parsers unchanged |
| Minimum-follow-notional | **`signal/follow_worthiness.py`** — thin **`FollowWorthinessGate`**; **`CopyStrategy`** calls it **after** `size()` and **before** `OrderIntent` (§4.2, missing-price rule locked) | **Not** embedded in sizing return types; **risk** unchanged |
| Token allowlist, entry/exit accept | **`GuruFollowEntryPolicy` / `GuruMirrorExitPolicy`** — unchanged for MVP | |
| `OrderIntent` + `risk.evaluate` + execution | **`CopyStrategy`** order of calls unchanged except **insert policy steps** | **`RiskPolicy` Protocol**, **`ConfiguredRiskPolicy`** semantics **unchanged** |
| Execution ports, order construction | **`execution/`** — **no C2 changes** | |
| Config | **`StrategySettings` + YAML`** (C2 knobs colocated with `copy_scale`) | **Risk YAML** unchanged for C2 MVP |
| Compose | **`guru_compose.build_guru_trading_node`** — pass new fields into `CopyStrategyConfig` / construct new sizing stack | |

**Composition:** Mirror today’s pattern: **construct policies in `CopyStrategy.__init__`** from `CopyStrategyConfig`, or inject via `set_*` only if tests require—prefer **frozen config + explicit constructor** like `_entry`, `_exit`, `_sizing`.

**Explicit split preserved**

- **Policy / signal:** “How much?” and “Worth copying?” → C2 sizing + min-notional skip → `copy_skip` with **distinct** `detail` / `ReasonCode` extension.
- **Risk:** “Safe and allowed?” → existing **`ConfiguredRiskPolicy.evaluate`** only.
- **Execution:** C2 **does not** change how intents become orders.

---

# 6. Recommended minimum implementation

**In scope (MVP)**

- Conviction-weighted effective scale: \(base\_scale \times \min(trade/avg, cap)\) with rolling average over last **K** guru trades (`size_raw` valid).
- Persistent **in-memory** rolling state per process (optional **future**: persist average across restarts — **out of MVP** unless trivial).
- `min_follow_notional_usd` skip **before** risk, with clear logging.
- Strategy YAML + `StrategySettings` + `CopyStrategyConfig` fields.
- Unit tests: sizing math, edge cases (cold start avg, missing `size_raw`, cap binding).

**Deferred post-MVP:** `c2_shadow_compare` (dual baseline vs C2 qty logging) — **not** in the first implementation slice; add only if a later change is nearly free and isolated.

**Out of scope (explicitly deferred)**

- Portfolio optimization, Kelly, multi-guru allocation, active rebalancing.
- Correlation- or venue-aware sizing (C3).
- Priority queues / burst ranking **unless** a later code review shows a **tiny** dependency—**not** MVP.
- Changing Phase B caps, `E_portfolio`, or capital gate logic.
- Persisted guru analytics DB or external feature stores.

---

# 7. Concrete code changes

| Area | Likely touch |
|------|----------------|
| **Sizing / policy** | `src/tyrex_pm/signal/sizing.py` — `SizingPolicy` protocol, `ProportionalSizingPolicy`, `ConvictionProportionalSizingPolicy`; `size(sig, *, branch)`; `record_accepted_entry_size(sig)` after entry `size()` when `size_raw > 0`. |
| **Min notional** | `src/tyrex_pm/signal/follow_worthiness.py` — **`FollowWorthinessGate`**. |
| **Strategy** | `src/tyrex_pm/strategy/copy_strategy.py` — order: accept → `size(..., branch=kind)` → `record_accepted_entry_size` **only for `kind=="entry"`** and positive `size_raw` → zero-qty check → gate → intent → risk. |
| **Reason codes** | `src/tyrex_pm/core/reason_codes.py` — add e.g. `MIN_FOLLOW_NOTIONAL` / `CONVICTION_SKIP` as needed for grep-friendly telemetry. |
| **Config** | `src/tyrex_pm/config/loaders.py` — `StrategySettings` + `load_strategy_settings` parse nested block e.g. `capital_allocation:` or flat keys `conviction_sizing_enabled`, … |
| **Strategy config** | `CopyStrategyConfig` in `copy_strategy.py` — mirror new fields. |
| **Compose** | `src/.../runtime/guru_compose.py` — passthrough from `StrategySettings` to `CopyStrategyConfig`. |
| **YAML examples** | `config/strategy/guru_follow.yaml` — commented C2 block with defaults off or conservative. |
| **Docs** | `Docs/CONFIG_MODEL.md`, `Docs/modules/signal/README.md`, `OPERATIONS.md` log table. |
| **Tests** | `tests/unit/` — new `test_conviction_sizing.py`, `test_min_follow_notional.py`; extend `test_copy_strategy_shadow.py` if behavior changes; `tests/test_split_config_loaders.py` for YAML. |

---

# 8. Config surface

**Proposed fields** (strategy YAML, mirrored on `StrategySettings` / `CopyStrategyConfig`)

| Field | Type | Default | Role |
|-------|------|---------|------|
| `conviction_sizing_enabled` | bool | `false` | Master switch: off → behavior matches today’s `ProportionalSizingPolicy(copy_scale)` only. |
| `conviction_sizing_cap` | float | `2.0` | Max multiplier \(\min(\cdot, cap)\); cap ≥ 1 typical; `1.0` disables upside skew. |
| `conviction_sizing_lookback_trades` | int | `20` | **K** for rolling mean of `size_raw`; **≥ 1** when conviction enabled. **Cold start (locked):** buffer excludes current trade when computing `size()`; empty buffer ⇒ treat **trade/avg ratio as 1.0** (effective scale = `base_scale`). After each accepted entry with `size_raw > 0`, append to deque (trim to K). |
| `min_follow_notional_usd` | float | `0` (disabled) | Skip when estimated `price_ref * qty` &lt; threshold; `0` = off. |

**Naming:** If nested `capital_allocation:` reduces YAML clutter, use that block consistently in loaders and docs.

**`copy_scale`:** Retains meaning as **`base_scale`** in the conviction formula when enabled; when disabled, remains global proportional scale as today.

---

# 9. Validation and success criteria

**Baseline:** Current production behavior with `conviction_sizing_enabled: false` and `min_follow_notional_usd: 0` (A/B switch).

**Metrics**

- **Capital utilization / return per dollar deployed** — operational (PnL / notional deployed); out of codebase scope but **success** requires ops comparison over matched windows.
- **Share of skipped tiny follows** — count `copy_skip` with C2 reason vs total entry candidates.
- **Average notional per accepted follow** — from `live_order_intent` / fills vs baseline period.
- **Conviction distribution** — log `effective_scale` / `trade_size/avg` (bounded) in shadow for sanity.

**Logging additions (MVP)**

- One **INFO** line when C2 skips for min notional: `event=copy_skip` + stable `reason_code` + `estimated_notional_usd=…`.
- Optional **DEBUG** for effective scale components during shadow tuning.

**Successful C2 experiment**

- Shadow or narrow live: **no regression** in risk denials unrelated to sizing; **no** duplicate submits; measurable **reduction** in tiny-follow churn; operator sign-off that **larger guru legs** receive proportionally more follower capital within caps; **no** increase in Phase B violations attributable to C2 (C2 should reduce tail of tiny orders, not break caps).

---

# 10. Rollout plan

0. **Baseline** — Freeze logs + config with C2 flags off; capture distribution of `qty`, intent notional, `copy_skip` reasons over representative window.

1. **Conviction sizing, shadow-first** — Enable `conviction_sizing_enabled: true` with `conviction_sizing_cap` conservative (e.g. `1.5`), `execution_mode: shadow`; compare logged `qty` / effective scale vs offline replay of baseline formula.

2. **Min-follow-notional** — Set `min_follow_notional_usd` to a **low** floor; verify `copy_skip` volume and no unintended interaction with missing `price_ref`.

3. **Live canary** — Enable on live with same risk YAML; monitor `risk_denied` mix and execution errors; tune `min_follow_notional_usd` and **K** / **cap** from data.

4. **Tune / document** — Lock defaults in `CONFIG_MODEL.md` and operator runbook.

---

# 11. Open questions / risks

- **Guru `size_raw` ≠ economic conviction** — large hedges or errors could overweight; cap mitigates but does not solve.
- **Rolling window** — short **K** = noisy ratio; long **K** = slow adaptation to guru regime change.
- **Cold start** — mitigated by ratio **1.0** when buffer empty (§8).
- **`min_follow_notional_usd` too high** — misses recoverable small edges; tune per wallet.
- **SELL / exit path** — **locked:** exits use **flat** `base_scale` only; no buffer updates on SELL.
- **Missing `price_ref`** — **locked:** policy skip when min-notional enabled (§4.2).
- **Interaction with `max_notional_usd_per_order`** — C2 increases some orders; caps still **clip** at risk; no conflict but **expect** more risk touches on large guru legs if caps tight.

---

# 12. Recommendation

**Validated statements**

1. **C2 MVP = conviction-weighted sizing + minimum-follow-notional only** — **Accepted.** Keeps scope testable and avoids C3/Phase B creep.

2. **Conviction proxy = guru trade size vs recent average trade size** — **Accepted** as the default MVP; capped and cheap; revisit only with data.

3. **`min_follow_notional_usd` = policy skip, not risk** — **Accepted**; implement **before** `OrderIntent` / `risk.evaluate` with dedicated telemetry.

4. **Reuse signal + compose patterns** — **Accepted**; extend `signal/`, `CopyStrategyConfig`, `guru_compose`, `StrategySettings` as today’s `copy_scale` does.

5. **Do not alter Phase B risk semantics** — **Accepted**; no changes to `ConfiguredRiskPolicy` contracts for C2 MVP.

6. **Validate against flat `copy_scale` baseline** — **Accepted**; feature flags (`conviction_sizing_enabled: false`, `min_follow_notional_usd: 0`) preserve baseline; optional manual log comparison (no `c2_shadow_compare` in MVP).

7. **Defer Kelly, multi-guru, optimization, execution-aware sizing, priority queues** — **Accepted** unless a future review finds a **minimal** dependency.

**C2 is ready** as the next Phase C implementation workstream after C1 stabilization.

**Exact MVP:** (a) rolling-average conviction scaling with YAML knobs + cap + lookback, (b) pre-risk min-follow-notional skip + logging, (c) tests + config/docs.

**First implementation slice (ordered):**

1. Add **`SizingPolicy`** protocol + **`ConvictionProportionalSizingPolicy`** (+ extend **`ProportionalSizingPolicy`** with `size(sig, *, branch)` / **`record_accepted_entry_size`** no-ops for protocol uniformity).
2. Wire feature-flagged fields through **`load_strategy_settings`**, **`StrategySettings`**, **`CopyStrategyConfig`**, **`guru_compose`**.
3. Add **`FollowWorthinessGate`** in **`signal/follow_worthiness.py`**; call from **`CopyStrategy`** after sizing, before **`OrderIntent`**.
4. Add **`ReasonCode`** values and structured **INFO** / **DEBUG** logging for C2 skips and diagnostics.
5. Add focused **unit tests** (sizing, gate, loaders, cold start, cap, missing price).
6. Update **CONFIG_MODEL**, **signal README**, **`guru_follow.yaml`** comments.

**Deferred:** §6 `c2_shadow_compare`, portfolio theory items, §11 residual tuning risks only.

---

*Parent doc for C2 tickets and detailed spec. Does not implement C2.*
