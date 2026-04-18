# Copy-strategy business scope to preserve (native PM rebuild)

This document restates **business behavior** Tyrex_PM’s guru-follow path was meant to provide. It is **not** a prescription to keep Nautilus types or class names—only the **operator- and quant-facing semantics** the native implementation must reproduce.

**Related:** [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) · [ARCHITECTURE.md](ARCHITECTURE.md)

---

## 1. Ingestion and signal path

### 1.1 Guru activity sources

**Native parity (locked):** the **only** authoritative guru mirror is **Polymarket Data API** incremental polling of the guru **proxy wallet** activity/trades. **Watermark**, **dedup**, and **gap-fill** are defined exclusively against API payloads (see [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) §1.1).

**Post-parity:** **RTDS** (or similar WebSocket wallet activity) may be added as a **latency accelerator** only after the project checks in a **frozen** subscribe/payload contract + fixtures; it **does not** replace Data API for recovery or parity tests.

**Historical note:** Tyrex v1’s `rtds_primary` / `rtds_shadow` / `poll_only` **switches** are **not** recreated for parity; the **business outcome**—reliable, deduped guru activity driving copy—is recreated via the **single Data API path** above.

### 1.2 Dedup, watermark, backfill

- **Dedup:** the same venue trade must not generate duplicate strategy-level processing (in-memory + persistent-enough keys if required across restarts—exact mechanism is implementation detail).
- **Watermark:** monotonic progress per guru stream so restarts and reconnects do not replay unbounded history unless explicitly requested.
- **Backfill / gap-fill:** after disconnect or lag, fetch missing activity from REST/Data API between last committed watermark and “now,” normalize, and feed through the **same** pipeline as live events (no second-class code path for “historical” rows except batching).

### 1.3 Token filter

- **Allowlist / denylist behavior:** restrict which outcome tokens the bot will consider for copy entries (and optionally exits), with clear logging when a guru trade is ignored for filter reasons.
- **Resolution / metadata hooks:** where the old system used market resolution or listing metadata to skip dead or invalid markets, preserve the **business outcome** (do not copy into untradeable states) via Gamma in the native stack when **`filters.exclude_untradeable_markets: true`** (Gamma `/markets` by `clob_token_ids`; default **off** so shadow/dev runs do not require Gamma).

### 1.4 Sizing

- **Copy scale:** a global or per-strategy multiplier that maps guru notional or size to **target** bot size before risk clamps.
- **Conviction sizing:** optional weighting of size based on guru “conviction” or similar signal-derived score; parameters must remain **tunable** (thresholds, min/max contribution, curve).
- **Static BUY entry sizing (optional):** when enabled in strategy YAML, BUY entries use a fixed **target USD notional** (`static_amount_usd` / limit price → size), ignoring copy scale and conviction; `intent_created` facts carry **`sizing_mode: static`** vs **`proportional`**.

### 1.5 Layer A–style filters (concept preservation)

Preserve the **roles** of these filter families even if code moves under `signals/` + `strategies/guru_follow/`:

| Concept | Business intent |
|--------|------------------|
| **Static amount / minimum significance** | Ignore guru trades that are too small in USD or size to matter; reduce noise and fee drag. |
| **Significance / conviction gating** | Require trades to pass a significance or conviction bar before mirroring. |
| **Exit interpretation** | Map guru sells / position reductions to **exit** or **reduce** intents with rules that match how operators expect copy behavior (e.g. full exit vs partial, dust handling). |
| **Token allowlist** | As in §1.3. |

Exact formulas can be ported from historical YAML/code as **reference math**, but the native system must expose the same **knobs** and **observability** (why a guru row was skipped).

---

## 2. Risk behavior

### 2.1 Order-level notional

- **Per-order minimum notional / size:** reject intents below trading or operational minimums (fail-closed with reason).
- **Per-order maximum notional / size:** cap single orders to limit tail risk and liquidity impact; **`notional.max_policy`** is **`deny`** (reject above max) or **`cap`** (clip size so notional ≤ max). **`risk_decision`** facts surface **`notional_max_policy`**, **`notional_capped`**, and **`notional_denied_above_max`** as applicable.

### 2.2 Deployment caps

- **Per-token deployment:** limit dollars or contracts deployed in a single outcome token (resting + filled basis per historical semantics—native design must define the accounting rule explicitly in the risk doc; parity means **operator-visible limits behave the same**).
- **Portfolio deployment:** aggregate cap across tokens/events consistent with the old “deployment budget” **intent** (protect total capital at risk).

### 2.3 Kill switch

- **Global halt:** configuration-driven immediate deny of new risk approvals (and optionally cancel resting bot orders—policy must be explicit).

### 2.4 Capital gate

- When enabled: require sufficient **balance** and **allowance** (or equivalent Polymarket prerequisites) before BUY approval; SELL path may depend on **inventory** not cash.

### 2.5 Inventory gate for SELL

- **No naked sells:** cannot sell more than venue-truth position (minus reserved in-flight) for that token; fail-closed with explicit reason when ambiguous.

### 2.6 Fail-closed posture

- Missing reference price, missing wallet snapshot freshness, inconsistent reconciliation state, or degraded health → **no new aggressive live risk approvals** unless policy explicitly allows reduced modes.

### 2.7 Reason codes and explainability

- Every deny (and material approve) should map to **stable reason codes** suitable for logs and structured facts (no “silent None”).

### 2.8 Concurrent-order and pacing controls

- Conceptual parity with **limits on overlapping guru-driven work**: max concurrent child orders, per-token serialization, or cooldowns—whatever the old system used to prevent stampedes should exist as **explicit policy** on the native `RiskEngine` or OMS admission layer (one place must own the behavior; see IMPLEMENTATION_PLAN).

---

## 3. Execution and runtime behavior

### 3.1 Live vs shadow / paper

- **Same strategy and risk code paths** should run in **shadow** (no venue submit) and **live** (submit via OMS), differing only by **execution backend** and **config flags**—preserving the old “shadow → live continuity” **idea** without Nautilus.

### 3.2 Startup readiness

- **Gating:** do not approve live aggressive intents until **minimum readiness**: authenticated session, initial wallet snapshot, subscribed user channel (or explicit degraded mode), and market data for pricing when required by risk.

### 3.3 Venue vs local truth and reconciliation

- **Venue truth:** positions, open orders, balances from Polymarket APIs / user stream.
- **Local truth:** in-flight orders, client order ids, retries, protection state.
- **Reconciliation:** periodic or event-driven comparison; explicit handling of **external** manual trades and orders.

### 3.4 Wallet snapshots and refresh

- **Positions, open orders, balances/allowances** refreshed on a schedule and on triggers (post-fill, reconnect, manual ops signal).

### 3.5 Reporting and structured facts

- **Run-scoped facts** (JSONL + manifest minimum) capturing guru signals, intents, risk decisions, OMS outcomes, reconciliation flags—suitable for CLI summarize and post-mortems.

### 3.6 Config-driven scenarios

- **Composed config:** strategy + risk + runtime + optional scenario overlay files, so operators can run named scenarios (live, shadow, reduced risk, etc.) without code changes.

---

## 4. Protection / overlays (post-parity)

Historical Tyrex added **virtual TP/SL** and **lot** tracking. For the **native rebuild**:

- **Copy-strategy parity does not include** virtual exits, TP/SL, or protection-driven cancels. Those ship in a **later** milestone under `protection/` (see [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) §13).
- When implemented, protection must emit **`ExitIntent` / `CancelIntent` through `RiskEngine`**—never direct CLOB calls from strategy or protection.

---

## 5. Non-goals for this scope document

- Defining Nautilus `Actor`/`Strategy`/`Cache` APIs.
- Mandating identical internal data structures to deleted v1 code.
- Preserving dead configuration keys that no longer map to meaningful Polymarket behavior (each key must be **migrated or dropped explicitly** in the config design section of IMPLEMENTATION_PLAN).
