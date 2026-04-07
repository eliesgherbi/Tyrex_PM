# End-to-end live flow: guru signal → execution outcome

**Business objective (evaluation lens):** follow the guru as faithfully as possible while adapting to a **smaller follower wallet** and enforcing **coherent** risk and execution rules—without accidental contradictions between “what we meant to copy” and “what actually reaches the venue.”

This document is **code-grounded** to the current Tyrex implementation. Paths refer to `src/tyrex_pm/` unless noted.

---

## 1. End-to-end flow overview

**Stage map (live `execution_mode: live`):**

1. **Ingest:** `GuruMonitorActor` / `GuruStreamActor` (and pipeline) produce a `GuruTradeSignal` and publish it on the Nautilus message bus under topic **`tyrex_pm.guru.GuruTradeSignal`** (`data/guru_monitor.py`: `GURU_TRADE_TOPIC`).
2. **Strategy:** `CopyStrategy.on_start` subscribes `_on_guru_trade` to that topic (`strategy/copy_strategy.py`). Each message runs **entry** (BUY) or **exit** (SELL) branch handling.
3. **Risk:** After strategy accepts sizing, `ConfiguredRiskPolicy.evaluate` runs (`risk/configured.py`) — per-order min/max deploy (**deny** vs **clip**/**bump**) then capital / token / portfolio / concurrent gates. On any deny, **`submit_intent` is never called.**
4. **Execution:** `NautilusGuruExecutionPort.submit_intent` resolves instrument, optional book C3 (guard / depth), mandatory instrument quantize, then `submit_order` (`execution/nautilus_guru_exec.py`).
5. **Lifecycle & fills:** Nautilus emits `OrderEvent`s; `CopyStrategy.on_order_event` → `emit_order_event_facts` writes **`order_lifecycle`** and **`fill`** facts (`reporting/order_events.py`). Venue denials surface as **`OrderDenied`** → lifecycle **`DENIED`**.

**Shadow mode difference:** compose wires `NoOpExecutionPort` for `execution_mode: shadow` (`runtime/guru_compose.py`). Risk still runs if a real policy is injected; execution does not submit to the venue.

---

## 2. Step 1 — Strategy / sizing

### 2.1 Where the signal enters

- Handlers publish `GuruTradeSignal` on **`GURU_TRADE_TOPIC`**.
- `CopyStrategy._on_guru_trade` receives the message; only `GuruTradeSignal` instances proceed (`copy_strategy.py` ~170–188).

### 2.2 Branch selection

- **`BUY`** → `GuruFollowEntryPolicy.evaluate` (`signal/entry.py`).
- **`SELL`** → `GuruMirrorExitPolicy.evaluate`.
- Other sides → log `copy_skip` with `ReasonCode.UNSUPPORTED_SIDE`; **no sizing, no risk, no execution.**

### 2.3 Entry / exit policy (token gate)

**Parameters:** `token_filter.enabled`, `token_filter.allowlisted_token_ids` → `CopyStrategyConfig` → `TokenFilterSpec` (`load_strategy_settings` / `copy_strategy.py`).

**Outcomes:**

| Outcome | Meaning | Next stages |
|--------|---------|-------------|
| `accept == False` | e.g. `NOT_ALLOWLISTED`, `MISSING_TOKEN_ID`, `COPY_SKIP` (wrong side on branch) | `strategy_decision` fact with `decision: skip`; **stop** |
| `accept == True` | `GURU_ENTRY_CANDIDATE` or `GURU_EXIT_MIRROR` | Continue to sizing |

**Business note:** Unfiltered mode (`enabled: false`) allows all tokens through the filter; faithfulness to “what guru traded” still depends on data API / RTDS producing the same token and side.

### 2.4 Sizing (`copy_scale`, conviction, formulas)

**Implementation:** `SizingPolicy` from `build_sizing_policy` (`signal/sizing.py`).

**Proportional (conviction off):**

- `quantity = max(0, guru_size_raw * copy_scale)` (`ProportionalSizingPolicy.size`).

**Conviction (conviction on, **entry** only):**

- Rolling buffer of **accepted BUY** `size_raw` values (last `conviction_sizing_lookback_trades` positive sizes).
- `ratio = min(trade_size / rolling_avg, conviction_sizing_cap)` (cold start: `ratio = min(1.0, cap)`).
- `effective_scale = copy_scale * ratio`.
- `quantity = max(0, guru_size_raw * effective_scale)`.
- **Exit** branch uses **`copy_scale` only** (no conviction ratio).

**Outputs for downstream:** `quantity` (float), plus metrics (`base_scale`, `effective_scale`, `conviction_ratio`, etc.) emitted in **`sizing`** fact when the path continues.

### 2.5 Zero quantity

If `qty <= 0` after sizing → `copy_skip` / `ReasonCode.COPY_SKIP` / `decision: skip`; **no risk, no execution.**

### 2.6 Per-order size (removed from here — risk only)

There is **no** strategy-stage minimum follow notional. After sizing, the strategy builds **`OrderIntent`** and **`ConfiguredRiskPolicy`** alone applies **`min_notional_usd_per_order` / `max_notional_usd_per_order`** with **`min_notional_policy` / `max_notional_policy`** (`deny` vs `cap`). Historical **`min_follow_*`** reason codes may still appear when reading **old** reporting artifacts.

### 2.7 Strategy acceptance: artifacts handed to risk

On full strategy accept (`copy_strategy.py` ~313–321):

- **`OrderIntent`:** `correlation_id` (guru `source_trade_id`), `token_id`, `side`, `quantity` (= sized qty), `signal_kind` (`entry`/`exit`), `price_ref` (guru signal price).

**Facts:** `strategy_decision` (accept), `sizing`, then after risk approve `execution_intent`.

### 2.8 Faithfulness vs business objective (strategy stage)

**Aligned:**

- **`copy_scale`** is the direct **capital-ratio knob**: follower size is guru size × scale (same outcome token), which matches “smaller wallet” as a **proportional** policy.
- **Token filter** is explicit operator control over universe.

**Potential distortions:**

- **Conviction sizing** intentionally **up-weights** large guru entries vs rolling average and **caps** at `conviction_sizing_cap`. That is **not** strict proportionality to each guru trade; it biases toward “larger-than-usual” guru conviction on entry.
- **Risk** may **clip** (max `cap`) or **bump** (min `cap`) deploy vs strategy-sized qty — see **`risk_decision`** / deploy-adjust metadata in reporting for “raw vs adjusted” visibility.

---

## 3. Step 2 — Risk / gates

**Entry point:** `ConfiguredRiskPolicy.evaluate` → `_apply_order_deploy_policies` (per-order min/max **deny** vs **clip**/**bump**) → `_evaluate_impl` → `_emit_risk_and_deployment` (facts always emitted for the evaluated intent, allow or deny).

**Quantity / notional:** `order_deploy = price_ref × quantity` when `price_ref` present (`_order_deploy_usd`). Strategy passes a **candidate** intent; risk may return an **adjusted** intent (quantity changed) when policies are **`cap`**, subject to feasibility (min bump vs max clip). Reporting should surface strategy vs risk-sized deploy / qty and clip/bump flags.

### 3.1 Ordered gate list (exact sequence)

**Phase A — per-order deploy (`_apply_order_deploy_policies`), before caps:**

| Step | Policy | Typical outcome |
|------|--------|------------------|
| Over `max_notional_usd_per_order` | `max_notional_policy: deny` | `RISK_ORDER_DEPLOYMENT_EXCEEDED` — stop |
| Over max | `max_notional_policy: cap` (default) | Clip qty so deploy ≤ max |
| Under `min_notional_usd_per_order` (BUY, min > 0) | `min_notional_policy: deny` (default) | `RISK_MIN_ORDER_NOTIONAL` — stop |
| Under min | `min_notional_policy: cap` | Bump qty so deploy ≥ min, if compatible with max — else `RISK_ORDER_DEPLOYMENT_INFEASIBLE` |

**Phase B — `_evaluate_impl`** (uses **adjusted** intent from phase A when approval continues):

| # | Gate | Condition (simplified) | Deny reason code | If deny: what never runs |
|---|------|------------------------|------------------|---------------------------|
| 1 | **Missing price (notional)** | `order_deploy is None` **and** `fail_on_missing_price_for_notional` | `RISK_MISSING_PRICE` | `execution_intent`, submit, lifecycle |
| 2 | **Capital gate bundle** | `_capital_gate_eval` | See §3.2 | Same |
| 3 | **Token deployment cap** | Only if `max_token_notional_usd_open` finite | See §3.3 | Same |
| 4 | **Portfolio deployment cap** | Only if `max_portfolio_notional_usd_open` finite | See §3.4 | Same |
| 5 | **Concurrent guru rests** | Only if `max_concurrent_guru_resting_orders` set | `RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT` (or deny if reader missing / count fails) | Same |
| 6 | **Approve** | All passed | `"approved"` (string, not `ReasonCode`) | `submit_intent` runs |

**Kill switch** is evaluated **before** deploy policies: `RISK_KILL_SWITCH` with no clip/bump.

**Important — ordering:** Per-order **deny** from deploy policies stops before token/portfolio math. After **clip**, later gates use the **reduced** deploy; after **bump**, later gates use the **increased** deploy.

**Missing price interaction:** If `price_ref` is missing, deploy policies may pass through unchanged; `_evaluate_impl` still enforces `fail_on_missing_price_for_notional` and downstream caps that need `order_deploy`.

### 3.2 Capital gate (`capital_gate_enabled` and related)

**When `capital_gate_enabled` is false:**

- If `collateral_reserve_usd > 0`, `_capital_gate_eval` returns **`RISK_ALLOWANCE_UNAVAILABLE`** (invalid config combination per loader; intended reserve path requires gate).
- Otherwise: **pass** without balance checks.

**When `capital_gate_enabled` is true** (`_capital_gate_eval` ~574+):

1. Require account snapshot provider; refresh if stale (`max_account_snapshot_age_seconds`). Missing / not present → `RISK_ACCOUNT_UNAVAILABLE`.
2. If any of `min_collateral_balance_usd`, `min_allowance_usd`, or `collateral_reserve_usd > 0` → require **py-clob** allowance snapshot (refresh per `max_allowance_snapshot_age_seconds`). Missing → `RISK_ALLOWANCE_UNAVAILABLE`.
3. Optional floors: balance → `RISK_INSUFFICIENT_COLLATERAL_BALANCE`; allowance → `RISK_INSUFFICIENT_ALLOWANCE`.
4. **Reserve (B4, BUY only):** if `collateral_reserve_usd > 0` and BUY: require `balance >= reserve + order_deploy` else `RISK_INSUFFICIENT_FREE_COLLATERAL_AFTER_RESERVE`. Missing notional → ** `RISK_MISSING_PRICE` **.

**Business meaning:** protects **live** collateral reality before submit; can deny trades that strategy still considered “sized correctly” vs guru.

### 3.3 Token deployment cap

**When:** `max_token_notional_usd_open` is finite.

**Check:** `token_deploy + order_deploy > cap` → `RISK_TOKEN_DEPLOYMENT_EXCEEDED`.

**`token_deploy`:** from `NautilusDeploymentBudget`: pending (resting leaves × limit) + filled cost basis on **that outcome token** (`runtime/deployment_budget.py`; policy via `fail_on_unresolved_token_deployment`).

**If budget missing** (e.g. shadow without reader): `RISK_TOKEN_DEPLOYMENT_UNRESOLVED`.

### 3.4 Portfolio deployment cap

**When:** `max_portfolio_notional_usd_open` finite.

**If budget missing:** `RISK_PORTFOLIO_DEPLOYMENT_UNRESOLVED`.

**If sum unresolved** per policy: `RISK_PORTFOLIO_DEPLOYMENT_UNRESOLVED`.

**If `order_deploy` None:** `RISK_MISSING_PRICE`.

**Else if** `portfolio_deploy + order_deploy > cap` → `RISK_PORTFOLIO_DEPLOYMENT_EXCEEDED`.

### 3.5 Concurrent guru resting orders

**When:** `max_concurrent_guru_resting_orders` is non-null.

**Check:** `count_guru_resting_orders_open >= limit` → `RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT`. If execution reader missing or count fails → **same deny** (fail-closed in code).

### 3.6 Risk stage — faithfulness vs objective

**Aligned:** Caps express **follower** wallet and exposure limits; guru-sized `order_deploy` is compared to **follower** deployment state, not guru’s wallet.

**Friction / drift:**

- **Strict ordering** can hide **which** cap would bind second—operators should read **`tyrex_risk_ops`** logs and reason codes.
- **Deployment math** uses **limit price × leaves** and **avg entry** for fills—not mark-to-market—so large market moves can change economic exposure while caps still look “unchanged.”
- **`min_notional_policy: cap`** can **increase** deploy vs strategy-sized qty; ensure token/portfolio caps still match intent.

---

## 4. Step 3 — Execution (resolve, quantize, submit, lifecycle)

### 4.1 Receiving an approved intent

**Live:** `NautilusGuruExecutionPort.submit_intent` (`execution/nautilus_guru_exec.py`).

**Immediate checks:**

- `mode != "live"` → **return** (no submit). *(Shadow uses `NoOpExecutionPort` in compose; this guard is a safety net.)*
- `price_ref is None` → `execution_outcome` **`outcome: error`**, `LIVE_ORDER_ERROR`, **no submit**.

**Approved quantity:** `approved_qty = float(intent.quantity)` — logging anchor; **depth clip** may reduce qty first; **instrument quantize** floors to size step and may adjust price to tick (never increases qty toward `min_quantity`). Risk is **not** re-run when execution reduces or snaps qty/price.

### 4.2 Instrument resolution

1. **Dynamic controller** (if wired): `resolve_and_activate(token_id)` → instrument in cache.
2. Else **static map** from runtime `polymarket_token_to_instrument` → `cache.instrument`.

**Failures → `execution_outcome` error, no submit:**

- Map exists but not in cache → `GURU_INSTRUMENT_NOT_IN_CACHE`
- Dynamic failure → `GURU_DYNAMIC_RESOLVE_FAILED` or `GURU_DYNAMIC_ACTIVATION_CAP`
- No map and no dynamic resolution → `GURU_INSTRUMENT_UNMAPPED`

### 4.3 Optional book path (`_c3_shape_prepare`)

**Runs when** entry guard **or** depth clip is enabled (runtime YAML).

**Sub-steps:**

1. Optional **book** (REST/cache) for guard / depth.
2. **`execution_book_strict` + missing book** → skip `EXEC_BOOK_UNAVAILABLE_SKIP` (`execution_outcome` **`stage: pre_submit_book`**).
3. **Entry guard** vs guru reference → `EXEC_ENTRY_GUARD_SKIP`.
4. **Depth clip** → may reduce `qty`; then **`floor_quantity_to_step`**; if qty rounds to **0** → `EXEC_INSTRUMENT_QUANTIZE_SKIP`.

**If book-path skip:** **`execution_outcome` `outcome: skip`**, optional **`normalization`** with `skipped_submit: true`. **No submit.**

### 4.4 Instrument quantize (always)

**Always** after §4.3 (or immediately after resolution if book path off): `quantize_limit_order_for_instrument` — tick / size step; **no** bump to `min_quantity`. If grid-fit qty violates venue `min_quantity` **without** enlarging past risk qty → **`EXEC_INSTRUMENT_QUANTIZE_SKIP`** (`stage: instrument_quantize`), **`execution_outcome` skip**, **`normalization`** fact. **No operator YAML** controls this; it is technical validity, not business policy.

### 4.5 Submit and execution facts

On success after quantize:

- Build **limit** GTC, `submit_order` **POLYMARKET** client id.
- Log `LIVE_ORDER_SUBMIT`.
- Emit `execution_outcome` **`outcome: submit`**, **`stage: framework_submit`**, **`risk_approved_not_success: true`** (risk pass **≠** copied trade).
- Emit **`normalization`** with `kind: instrument_quantize` only when quantize **changed** qty or price vs pre-quantize values.
- Optional **limit timeout** cancel → `EXEC_LIMIT_TIMEOUT_CANCEL`.

### 4.6 Lifecycle vs risk vs submit (business clarity)

| Concept | Meaning |
|--------|---------|
| **Risk passed** | `ConfiguredRiskPolicy.evaluate` approved the **risk-adjusted** `OrderIntent`. **Does not** mean the venue accepted or filled. |
| **Pre-submit skip** | **`execution_outcome` `outcome: skip`** — book guard, book missing, or **`exec_instrument_quantize_skip`**. |
| **Submit happened** | **`execution_outcome` `outcome: submit`** — Tyrex called `submit_order`; still not “filled.” |
| **Lifecycle SUBMITTED / ACCEPTED** | Engine/venue path via `order_lifecycle`. |
| **Lifecycle DENIED / REJECTED** | Post-risk venue/engine rejection (balance, rules, etc.). |
| **Fill count 0** | Possible **resting** limit, **canceled**, **denied**, or slow market — compare `order_lifecycle`, `fill`, and **`execution_outcome`** histograms. |

**Operator lens:** Tune **risk** `min_notional_*` so post-quantize orders are usually valid; if quantize skips spike, sizes are likely below venue `min_quantity` after stepping — raise risk mins or scale, not an “alignment mode.”

---

## 5. End-to-end truthfulness assessment

| Stage | Helps faithful guru follow? | Introduces bias / drift? |
|-------|------------------------------|---------------------------|
| **Strategy** | Proportional scale matches “smaller wallet” narrative. | Conviction biases size; filter restricts universe. |
| **Risk** | Caps align follower exposure to **their** book; per-order **clip**/**bump** keeps deploy inside configured bounds. | Ordering hides secondary failures; **deny** policies can still block “proportionally tiny” guru trades; reserve requires **accurate** CLOB snapshot. |
| **Execution** | Internal tick/step snap keeps limits **expressible** on the instrument. | Floors **reduce** qty vs raw intent; quantize skip or lifecycle **DENIED** can still block a “risk-approved” follow. |

---

## 6. Key corrections to consider (prioritized for “faithful follow”)

1. **Operator clarity** — **risk** USD deploy policy vs **internal** execution quantize (tick/step/min-q without bump) vs optional **C3** book features — read `execution_outcome.stage` and `risk_decision` deploy metadata.
2. **Conviction defaults** — if the primary goal is **strict proportionality**, keep conviction **off** or document conviction as an **alpha overlay** on top of copy fidelity.
3. **Cap evaluation transparency** — consider emitting which gate **would** bind next on deny (debug/ops only) to offset “first failure wins” obscurity.
4. **Dynamic resolution failures** — guru may trade a token before follower cache has it; failures look like “we didn’t follow” though guru did—warmup / retry policy is operational debt.
5. **Limit orders & fills** — faithful **directional** follow with GTC limits still allows **zero fill** if price moves; business “faithfulness” may need measure vs **guru fill price** / latency, not only submit.

---

## 7. File index (quick reference)

| Concern | Primary modules |
|--------|-----------------|
| Bus / signal | `data/guru_monitor.py`, `data/guru_stream_actor.py`, `data/guru_ingest_pipeline.py` |
| Strategy | `strategy/copy_strategy.py` |
| Entry / exit policy | `signal/entry.py` |
| Sizing | `signal/sizing.py` |
| Risk policy | `risk/configured.py` |
| Deployment sums | `runtime/deployment_budget.py` |
| Live submit | `execution/nautilus_guru_exec.py` |
| Venue math | `execution/c3_normalize.py`, `execution/c3_depth.py`, `execution/c3_entry_guard.py` |
| Reason codes | `core/reason_codes.py` |
| Lifecycle facts | `reporting/order_events.py` |
| Compose | `runtime/guru_compose.py` |
| Config | `config/loaders.py` |
