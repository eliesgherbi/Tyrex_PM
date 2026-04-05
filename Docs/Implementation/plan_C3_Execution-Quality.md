# Plan: C3 — Execution Quality

Parent planning document for **Phase C / C3**: improve **how approved follow intents are expressed at the Polymarket venue** to reduce implementation loss, **without** changing **C2** capital-allocation ownership or **Phase B** risk ownership.

---

## Locked MVP policy (implementation)

### A. First implementation target — **Validated**

- **MVP applies to the framework-submit path only:** `NautilusGuruExecutionPort` (`execution/nautilus_guru_exec.py`) + Nautilus `submit_order` / `Cache` / `clock` timers.
- **Legacy `PolymarketExecutionPolicy` (py-clob)** C3 parity is **deferred (second phase)** unless a follow-on change is nearly free.
- **Code-based reasoning:** Timeout/cancel uses `Strategy.clock.set_timer` + `Strategy.cancel_order` (Nautilus examples: TWAP algorithm), order lifecycle visibility lives in `Cache`, Phase B B3 guru-order tagging is already framework-centric; py-clob has no shared timer/cancel wiring in Tyrex today.

### B. Normalization — quantity never above risk intent — **Validated**

- **Do not increase `quantity` above the risk-approved `OrderIntent.quantity` in C3 MVP.**
- Price may be rounded/ticked **within venue rules** (typically quantize to `price_increment`).
- Quantity may be **rounded down / clipped** to size step; depth clip only **reduces** size.
- If **min size** or **min notional** (including `TYREX_MIN_BUY_NOTIONAL_USD` for BUY) **cannot** be met without **increasing** quantity above the approved intent → **skip submit** with **`EXEC_VENUE_NORMALIZE_SKIP`** (or dedicated subreason in logs).
- **Rationale:** avoids hidden re-risk; conservative execution shaping; Phase B approved an upper bound on size.

### C. Book source — **Validated**

- **Primary:** `Cache.order_book(instrument_id)` when `Cache.has_order_book(instrument_id)` is true (Polymarket L2 must actually be subscribed/streamed for this to be non-empty in live).
- **Optional fallback (feature-flagged, default off):** one REST snapshot via existing `ClobClient.get_order_book(token_id)` pattern (`data/resolution.py`), only when `execution_book_rest_snapshot_enabled: true` in runtime YAML — **cheap, already used in-repo** for resolution/smoke.
- **If** entry guard or depth clip **requires** top-of-book and **no** book is available: behavior is governed by `execution_book_strict` (see §8). **Never** assume a book exists without a successful snapshot; always **log** book-missing paths at INFO/DEBUG.
- **Reject:** silent fallback to “reference price only” for guard without logging.

### D. Timeout / cancel scope — **Validated**

- **MVP implements timeout + cancel only on the framework path** (`NautilusGuruExecutionPort`). Uses `clock.set_timer` + `cancel_order` when the order is still open after `execution_limit_timeout_seconds`.
- If integration testing shows **fragile** cancel semantics, **stage** timeout behind normalization + entry guard (flags) rather than shipping broken cancels; document partial state in implementation notes / `OPERATIONS.md`.
- **Thin strategy glue:** `CopyStrategy` may forward `on_order_event` to the execution port **only** to cancel timers when orders close (execution-owned behavior; strategy does not interpret the book).

### E. First implementation slice — explicit order

1. Feature-flagged **runtime** config, **all default off** (preserve current behavior).
2. Shared **`venue_normalize`** helper (pure + instrument).
3. Framework-path integration in **`NautilusGuruExecutionPort`** (orchestration only).
4. Structured **`event=`** logs + **`ReasonCode`** additions for execution quality.
5. **Entry price guard** (canonical unit: **ticks** vs `instrument.price_increment`) behind flag.
6. **Basic depth clip** (single-level **best bid/ask size** proxy MVP) behind flag, gated on book availability rules.
7. **Limit timeout + cancel** behind flag on framework path.
8. **Tests** + **CONFIG_MODEL** / runtime YAML comments + **OPERATIONS** grep notes.

---

## 1. Objective

C3 improves follower **alpha capture after a follow decision** by reducing **implementation loss** during execution: fewer bad prints against a moved market, more disciplined limit placement, respect for book liquidity and venue increments, and pre-submit normalization—while keeping **policy/signal (C2)** and **risk (Phase B)** boundaries intact. Execution remains the owner of **venue expression**.

---

## 2. Why C3 is next

**C1** reduced **detection latency** (event-driven ingest). **C2** improved **how much** to follow and **which** opportunities pass a policy floor. The remaining leak on many edges is **execution naïveté**: the system can still send a **limit at the guru’s historical trade price** into a book that has already moved, hold **GTC** rests with **no time-boxed lifecycle**, ignore **visible depth** when sizing, and rely on **venue rejects** or env floors instead of systematic pre-trade shaping. **C3** addresses that layer **after** risk approves an `OrderIntent`.

---

## 3. Current workflow audit

**Intent path (code-grounded)**

1. **`CopyStrategy._handle_branch`** (`src/tyrex_pm/strategy/copy_strategy.py`): after C2 sizing + `FollowWorthinessGate`, builds **`OrderIntent`** (`core/types.py`) with `quantity`, `price_ref=sig.price_raw`, `token_id`, `side`, `signal_kind`, `reason_code`.
2. **`ConfiguredRiskPolicy.evaluate`** (`src/tyrex_pm/risk/configured.py`) (or shadow pass-through): **unchanged** Phase B gates; if denied → `copy_skip` / `risk_denied`.
3. **`ExecutionPort.submit_intent(intent, mode=...)`** (`src/tyrex_pm/execution/port.py` protocol).

**Live execution implementations**

| Path | Class | Module | Behavior |
|------|--------|--------|----------|
| **Framework submit** (preferred live) | `NautilusGuruExecutionPort` | `execution/nautilus_guru_exec.py` | `order_factory.limit` at **`intent.price_ref`** and **`intent.quantity`**; **`TimeInForce.GTC`**; `submit_order(..., POLYMARKET_CLIENT_ID)`. Requires resolved `InstrumentId` / `BinaryOption` (static map or `GuruInstrumentDynamicController`). |
| **Legacy py-clob** | `PolymarketExecutionPolicy` | `execution/polymarket_policy.py` | `ClobClient.create_and_post_order(OrderArgs)` — **limit** at **`price`/`size`** from intent; sync HTTP. |
| **Shadow / tests** | `NoOpExecutionPort` | `execution/port.py` | Records intent only. |

**“Naive” in practice**

- Both live paths are **limit orders at the guru signal price**, not aggressive market IOC. They are still **naive** for C3 purposes: **no comparison** to current best bid/ask or microstructure at submit time, **no** operator-defined slippage band around the guru reference, **`GTC`** on the framework path (rest until cancel or fill), **no** timeout/cancel automation tied to guru follow, **no** book-depth-based size clip inside execution (C2 does not use the book).

**Venue minimum / notional (today)**

- **`TYREX_MIN_BUY_NOTIONAL_USD`** (env, default `1`): **BUY** only — if `price * qty` below floor, **`NautilusGuruExecutionPort`** and **`PolymarketExecutionPolicy`** **log** `LIVE_ORDER_ERROR` and **return without submit** (`nautilus_guru_exec.py` ~94–101, `polymarket_policy.py` ~60–70). **No** automatic quantity bump to meet minimum (contrast: `examples/order_lifecycle_smoke.py` documents a smoke-only bump pattern).
- **C2** `min_follow_notional_usd` (`signal/follow_worthiness.py`) is a **policy floor before intent**; **Phase B** caps remain **ceilings / exposure** in `configured.py` — orthogonal to tick/min-size normalization.

**Order book / market state**

- **Guru path:** `CopyStrategy` does **not** subscribe to order books; it only consumes **`GURU_TRADE_TOPIC`**.
- **Elsewhere:** `data/resolution.py` uses **`ClobClient.get_order_book(token_id)`** for **slug/market resolution** and `book_check.summarize_book_sides` — **not** wired into guru submit.
- **Framework path:** `NautilusGuruExecutionPort` holds **`self._strategy.cache`** — in principle **L2 books** may exist if Polymarket data factories feed the `Cache`; this must be **confirmed per deployment** before relying on book snapshots at submit time (see §11).

**Limit lifecycle / cancel / timeout**

- **No** Tyrex guru code today schedules **cancel-after-T** or monitors **partial fill** for guru orders. Orders are **fire-and-submit** (framework) or **single POST** (legacy). Any cancel/expire behavior would be **new C3 work** (likely `Strategy.cancel_order` / Nautilus events — **ticket-level design**).

**Logs (execution-relevant)**

- **Strategy:** `shadow_order_intent`, `live_order_intent` (qty, side, latencies) — `copy_strategy.py`.
- **Framework exec:** `LIVE_ORDER_SUBMIT`, `LIVE_ORDER_ERROR` (`ReasonCode`) — `nautilus_guru_exec.py`.
- **Legacy exec:** same reason strings via `polymarket_policy.py` logging.
- **Risk:** `tyrex_risk_ops` / `copy_skip` with `risk_denied` — unchanged for C3 design center.

**Compose wiring**

- `build_guru_trading_node` (`runtime/guru_compose.py`) attaches **`NautilusGuruExecutionPort`** when `execution_mode == live` and `polymarket_framework_submit`, else **`PolymarketExecutionPolicy`** with `build_clob_client_from_env(runtime)`.

---

## 4. C3 target behavior

### 4.1 Entry price guard

**Intent:** Immediately **before** venue submit, compare **actionable entry price** (e.g. best ask for BUY copy) to **`intent.price_ref`** (guru reference). If the market has moved **worse** than a configured tolerance (cents or relative), **do not submit**; log a dedicated **execution-quality skip** (not `risk_denied`, not C2 `copy_skip`).

**Ownership:** **Execution-quality protection** — prevents paying an obviously stale limit into a dislocated book. It is **not** a portfolio exposure decision (risk) and **not** “should we copy?” (C2). It belongs at the **execution boundary** inside or directly behind `ExecutionPort`, using live book/quote data available there.

### 4.2 Limit orders with timeout

**MVP:** Keep **limit** expression (already the default), but place the limit at a **price inside an allowed slippage band** derived from guru reference + current top-of-book (not necessarily exactly `price_ref`). **Wait** a bounded **`limit_timeout_seconds`**; if **not filled** (or not filled to policy—MVP may be “any fill” vs “full fill” — specify in implementation spec), **cancel** the working order.

**Why this step:** Smallest advance beyond **GTC @ guru price forever**: bounds adverse selection from resting too long and defines **explicit failure mode** (timeout) vs silent non-fill.

**Does not solve:** Optimal queue position, adverse selection from information leakage, or multi-level passive ladders. **Defers** TWAP, iceberg, smart SOR.

### 4.3 Book-depth-aware sizing

**MVP:** At submit time, estimate **visible liquidity** at/inside the chosen limit level (e.g. cumulative ask size up to limit price for BUY). If **intent quantity** exceeds **`book_depth_utilization_cap` ×** that liquidity (configurable ratio), **reduce** submit size (clip) **before** submit. Log **intended vs submitted** quantity.

**Ownership:** **Execution-time shaping** of an already risk-approved size. **Not** a replacement for C2 conviction or min-follow-notional; C2 answers “portfolio/policy meaningful size,” C3 answers “what the visible book can absorb without absurd immediate market impact at this price.”

### 4.4 Venue normalization

**MVP:** Before submit: **round price** to **tick** (`instrument.price_increment` / CLOB tick), **round quantity down** to **min size / step**, and check **min notional** feasibility. **Do not** increase quantity above the risk-approved intent (see **Locked MVP policy §B**). If minimums cannot be met conservatively → **skip** with **`EXEC_VENUE_NORMALIZE_SKIP`**.

**Distinction:** **C2** `min_follow_notional_usd` = policy “worth doing.” **Phase B** = caps and gates. **C3 normalization** = **feasibility** of the order as the venue will accept it, aligned with `BinaryOption`/instrument metadata where available.

---

## 5. Architectural placement

| Concern | Owner | Notes |
|--------|--------|--------|
| Entry price guard | **Execution** (port or small helper used only by port) | May read `Cache` order book or one-off CLOB read; **must not** live in `CopyStrategy`. |
| Limit price + **timeout/cancel lifecycle** | **Execution** | Prefer **framework** path: strategy/port uses `submit_order`, timers/events, `cancel_order`; legacy path may need **async worker** or simplified “submit + blocking wait + cancel” **only if** acceptable for MVP. |
| Book-depth-aware clip | **Execution** | Same data source as guard; adjust `quantity` on a **copy** of intent or internal submit DTO; **do not** mutate risk’s view of “approved intent” retroactively—either re-evaluate risk on clipped qty (see §11) or document **risk evaluates full intent first** and clip is execution-only with conservative assumptions. |
| Tick / min size / min notional | **Execution** | Shared helper usable by **both** `NautilusGuruExecutionPort` and `PolymarketExecutionPolicy`. |
| C2 policy / sizing | **Unchanged** | `signal/sizing.py`, `signal/follow_worthiness.py`. |
| Phase B risk | **Unchanged** | `risk/configured.py`; C3 must not reimplement caps. |
| `OrderIntent` schema | **Prefer unchanged** for C3 MVP if possible | Execution derives **submit price/qty** from intent + book; if schema extension is unavoidable, keep minimal (e.g. optional `execution_hints` later—not required in parent plan). |

**Preserve split:** policy/signal = worth following / how much (policy); risk = safe/allowed; execution = how to express at venue.

---

## 6. Recommended minimum implementation

**In scope (MVP)**

- Entry price guard (configurable, default-off).
- One **limit-with-timeout** path on the **primary** live path first (`polymarket_framework_submit: true`), with legacy path **feature-parity or documented second phase** if wiring cost is high.
- One **depth utilization cap** rule (simple cumulative depth vs clip).
- Pre-submit **normalization** (tick, min size, min notional bump or skip-with-reason).
- **Runtime-oriented config** (alongside existing `RuntimeSettings` / YAML) — see §8.
- **Validation metrics** via structured logs (§9); **no** dedicated analytics platform.

**Explicitly out of scope**

- TWAP / VWAP / multi-venue SOR, queue games, adversarial flow detection.
- Cross-venue routing; full smart order routing.
- **Reporting module** / execution analytics warehouse.
- Moving C3 logic into **strategy** or **risk**.
- Replacing **C2** with execution-time “conviction.”

---

## 7. Concrete code changes

**Likely touched modules**

- `src/tyrex_pm/execution/nautilus_guru_exec.py` — main C3 hook for framework path: pre-submit guard, normalized price/qty, possibly **GTD** / timer / cancel; richer logging.
- `src/tyrex_pm/execution/polymarket_policy.py` — parallel behavior for py-clob path where MVP requires it.
- **New** `src/tyrex_pm/execution/` helpers (suggested): `venue_normalize.py`, `entry_guard.py`, `book_depth_clip.py` (pure functions + instrument/book inputs) to keep ports thin.
- `src/tyrex_pm/config/loaders.py` — extend **`RuntimeSettings`** (or small nested **ExecutionQualitySettings** frozen dataclass loaded from runtime YAML) with C3 fields.
- `config/runtime/*.yaml` — document defaults; **example** C3 block comments only.
- `src/tyrex_pm/runtime/guru_compose.py` — pass C3 config into execution port constructors.
- `src/tyrex_pm/core/reason_codes.py` — stable strings for **execution skip** / **timeout cancel** (e.g. `EXEC_ENTRY_GUARD_SKIP`, `EXEC_LIMIT_TIMEOUT_CANCEL`) — exact names TBD in implementation.

**Tests**

- Unit: normalization (tick, size step), guard math (edge at boundary), depth clip formula (mock book).
- Integration: mock `Cache` book / mock client — `NautilusGuruExecutionPort` submit vs skip; legacy path if implemented.
- Regression: C3 **disabled** → behavior matches **current** submit (price_ref, GTC, no extra skips).

**Compose**

- No changes to **risk** or **strategy** for MVP beyond optional **log correlation** if execution emits new events (still keyed by `correlation_id`).

---

## 8. Config surface

**Runtime YAML** fields (defaults **off**; canonical slippage unit **ticks**):

| Field | Type / default | Purpose |
|-------|------------------|--------|
| `execution_venue_normalize_enabled` | bool, `false` | Tick / size-step / min-notional feasibility (no qty bump above intent). |
| `execution_entry_guard_enabled` | bool, `false` | Entry price guard vs top-of-book. |
| `execution_max_entry_slippage_ticks` | int, `0` | Max **ticks** market may move **against** follower vs `intent.price_ref` (0 = disabled guard math when guard enabled — validate in loader). |
| `execution_book_depth_clip_enabled` | bool, `false` | Clip size vs **best** bid/ask size × cap (MVP single-level). |
| `execution_book_depth_utilization_cap` | float (0,1], `1.0` | Max fraction of observed top-of-book size. |
| `execution_book_rest_snapshot_enabled` | bool, `false` | If no `Cache` book, allow one **REST** `get_order_book` snapshot (same pattern as `data/resolution.py`). |
| `execution_book_strict` | bool, `false` | If **true** and guard/clip needs book but snapshot fails → **skip order** with reason; if **false** → **no-op** guard/clip and log. |
| `execution_limit_timeout_enabled` | bool, `false` | Timer + cancel unfilled working order (framework only). |
| `execution_limit_timeout_seconds` | float, `30` | Seconds until cancel attempt. |

**Environment:** **`TYREX_MIN_BUY_NOTIONAL_USD`** remains the BUY notional floor check; normalization **skips** (does not bump) when infeasible without exceeding intent qty.

---

## 9. Validation and success criteria

**Compare against baseline:** current **`NautilusGuruExecutionPort`** / **`PolymarketExecutionPolicy`** behavior (limit @ guru price, GTC, env min-notional drop).

**Metrics (log-derived MVP)**

- **Entry slippage proxy:** distribution of (`fill_price - guru_price_ref`) or (`submit_limit - reference`) for fills/submits.
- **Fill rate:** intents that achieve ≥ partial fill within timeout vs **timeout cancel**.
- **Entry guard skip rate:** count **`EXEC_*_SKIP`** / total guru intents reaching execution.
- **Timeout cancels:** count per session.
- **Submitted vs intended size:** ratio when depth clip applies.

**Successful experiment**

- Shadow-mode **simulation** may be limited (no fills); **narrow live canary** with small risk caps: measurable **reduction in bad prints** (guard + band) or **lower tail slippage** without collapsing fill rate below an operator-defined floor. **No** requirement for full win-rate lift in MVP—**implementation quality** improvement is sufficient if documented.

---

## 10. Rollout plan

1. **Phase 0 — Baseline:** Capture logs + `guru_primary_report.py`-style notes on current **LIVE_ORDER_SUBMIT** / errors / resting behavior.
2. **Phase 1 — Entry guard only:** Enable guard in **live canary**; tune `execution_max_entry_slippage_*`; measure skip rate vs false negatives.
3. **Phase 2 — Limit + timeout:** Introduce **bounded lifetime** (GTD where supported, or submit + **timer + cancel**); validate no orphaned orders, no Phase B B3 surprises (`state_readers` guru rest counts).
4. **Phase 3 — Depth clip + normalization:** Enable clip + tick/size bumps; verify interaction with **`TYREX_MIN_BUY_NOTIONAL_USD`** and risk **max_notional** (clipped qty may require **re-risk** policy decision—see §11).
5. **Phase 4 — Production default:** Enable suite for framework path; legacy path parity or supported matrix in `OPERATIONS.md`.

---

## 11. Open questions / risks

- **Book freshness:** Is `Cache` L2 reliable at **exact** submit moment for dynamically activated instruments? If not, add **REST book snapshot** (like `get_order_book`) with latency tradeoff.
- **Risk re-evaluation:** If execution **clips** quantity **after** risk approved full size, notional caps could be **over-conservative**; options: (**a**) risk on clipped size only (requires book before risk—architectural drift), (**b)** risk on intent then **clip only if clipped size still ≤ approved** (usually true if clip reduces qty), (**c)** fast **second risk check** on clipped intent—**preferred** if clip is material.
- **GTC → timed lifecycle:** Nautilus **cancel** semantics and **client order id** stability must be verified; avoid duplicate submit on retries.
- **Guru price vs local reference:** `price_ref` may not equal **your** achievable price; guard bands must be **tuned** to avoid over-skipping in fast markets.
- **Thin books:** Depth clip may drive size **below** C2 or venue min notional → skip vs bump—define deterministic rule.
- **Dual execution paths:** Framework vs py-clob **feature parity** cost—may stage C3 on framework first.

---

## 12. Recommendation

**Validated recommendations (explicit)**

1. **C3 MVP scope** — **Validated:** entry price guard, limit with timeout, basic book-depth-aware sizing, venue normalization form the right **minimum** set; advanced algos deferred.
2. **Execution-owned** — **Validated:** C3 stays in **`execution/`** and ports; not strategy, not risk.
3. **Entry guard = execution quality** — **Validated:** not a Phase B gate; distinct reason codes / logs.
4. **Depth sizing ≠ C2** — **Validated:** execution-time shaping only; C2 remains policy allocator.
5. **Pre-submit normalization** — **Validated:** primary mechanism should be deterministic normalize/bump/skip-with-reason; venue reject is fallback signal only.
6. **Baseline comparison** — **Validated:** all C3 claims should be measured vs current **`nautilus_guru_exec` / `polymarket_policy`** behavior.
7. **Advanced execution** — **Validated:** TWAP, SOR, queue optimization explicitly **deferred**.

**Program recommendation**

- **C3 is ready to be planned as the next Phase C implementation workstream** after C2 validation settles, with **framework-submit path** as the **first implementation target**.
- **Exact MVP:** §6 bullet list (guard + limit/timeout + depth clip + normalize + config + logs).
- **Deferred:** reporting platform, advanced algos, cross-venue, passive microstructure games.
- **First implementation slice:** follow **Locked MVP policy §E** (framework path only; legacy deferred per §A).

---

*Document version: parent plan for C3; detailed API/event design belongs in a child spec after Nautilus cancel/timer proof-of-concept.*
