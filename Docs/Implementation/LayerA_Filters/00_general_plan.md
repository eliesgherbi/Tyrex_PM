# Layer A — General implementation plan (filter architecture)

**Status:** planning only — no production code in this change set.

**Related repo truth:** `Docs/Architecture.md`, `Docs/CONFIG_MODEL.md`, `config/strategy/guru_follow.yaml`, `config/risk/guru_follow_risk.yaml`, `config/runtime/live_polymarket.yaml`.

**Roadmap / boundary alignment:** Layer A follows the principles in **`Docs/Architecture.md`** (thin `CopyStrategy`, policy in `signal/`, Nautilus-first wiring) and the same separation of concerns documented in **`Docs/README.md`** / **`Docs/CONFIG_MODEL.md`**: **follow-policy and signal gating** live in strategy-side modules; **capital, deployment budget, allowance, and framework-backed portfolio truth** stay in **`risk/`** and **`runtime/`** (see `runtime/state_readers.py`). Phased backlog items may be tracked in **`Docs/Implementation/road_map.md`** when that file is maintained alongside **`Docs/Implementation/current_state.md`**; this plan does not duplicate risk or execution responsibilities.

---

## 1. Objective of Layer A

**Layer A** is the **signal-side** layer that sits between **`GuruTradeSignal`** arrival and **sizing → `OrderIntent` → risk → execution**.

It provides a **single composable framework** for two **kinds** of rules (see §3):

1. **Gating** — should this signal proceed?
2. **Interpretation** — how should we interpret this signal **before** sizing (especially exit quantity semantics)?

Goals:

- Class-based, configurable filters under **`src/tyrex_pm/signal/`** (new `layer_a` package).
- **No** embedding of portfolio math, balances, or deployment caps inside strategy code; **injected read-only context** from **`runtime/`** where follower position is required.
- Testable units: pure gating filters + interpretation filters with stubbed **`LayerAContext`**.

---

## 2. Two kinds of Layer A rules

### 2.1 Gating filters

**Question:** *Should this signal proceed to sizing?*

- **Output:** `accept=False` stops the pipeline (log + facts); `accept=True` continues.
- **Examples (v1):** token allowlist (adapted from existing `token_filter`), static USD significance, significance conviction (median vs prior BUY notionals).
- **No** change to how quantity is derived **unless** combined with interpretation metadata elsewhere (gating alone is pass/stop only).

### 2.2 Interpretation filters

**Question:** *Given we proceed, how do we map this signal to intent semantics before sizing?*

- **Output:** `accept=True` with **`metadata`** consumed downstream (e.g. exit quantity mode). Interpretation steps may set `accept=False` if interpretation is impossible (e.g. `full_exit` but position cannot be resolved — **fail closed**, no silent fallback to mirror).
- **Example (v1):** `exit_filter` with `exit_method: full_exit` — does not “gate” in the sense of significance; it **redefines exit qty derivation** (see `01_exit_filter_plan.md`).
- **Important:** interpretation filters **must not** pull `Cache`/`Portfolio` inside `signal/`; they call **`LayerAContext`** only.

### 2.3 One framework

Both kinds implement the same **`LayerAFilter`** protocol and return the same **`LayerAOutcome`** (§5). The orchestrator distinguishes **pipeline lists** (entry gating vs exit interpretation) and **merges metadata** from interpretation steps **only** when all required steps accept.

---

## 3. Current state review (verified call flow)

### 3.1 Guru ingestion → message bus

- **`GuruSignalPipeline.try_publish`** (`src/tyrex_pm/data/guru_ingest_pipeline.py`): dedup, **`msgbus.publish`**, watermark, optional `guru_signal` fact.
- **`GuruMonitorActor`**, **`GuruStreamActor`** — shared dedup/watermark via **`guru_compose.py`**.

### 3.2 Strategy

- **`CopyStrategy.on_start`**: subscribe **`GURU_TRADE_TOPIC`** → **`_on_guru_trade`** (`src/tyrex_pm/strategy/copy_strategy.py`).
- **`_on_guru_trade`**: `BUY` → **`_handle_branch(..., "entry")`** with **`GuruFollowEntryPolicy.evaluate`**; `SELL` → **`"exit"`** with **`GuruMirrorExitPolicy.evaluate`**.
- **`_handle_branch`**: policy accept → **`_sizing.size`** → **`OrderIntent`** → **`_risk.evaluate`** → **`_execution.submit_intent`**.

### 3.3 Config today

- **`StrategySettings`**: required top-level **`token_filter`**, plus `copy_scale`, conviction, dedup path, optional `bot_sell_validate` (`src/tyrex_pm/config/loaders.py`).
- **`build_guru_trading_node`** wires **`CopyStrategyConfig`** and **`ConfiguredRiskPolicy`**.

---

## 4. Insertion point (v1)

- **Replace** direct calls to **`GuruFollowEntryPolicy` / `GuruMirrorExitPolicy`** with **`LayerAOrchestrator.run(...)`** returning **`LayerAOutcome`**.
- **Token gate:** continue to load from **top-level `token_filter` only** (§8); internally build a **`TokenAllowlistGatingFilter`** from **`TokenFilterSettings`** — **no** YAML move for operators.
- **After** orchestrator returns **`accept=True`**, apply **interpretation metadata** to the sizing branch (e.g. skip proportional exit sizing when `exit_qty_mode=full_position` and qty comes from context).
- **Risk and execution** unchanged in responsibility.

---

## 5. Result model — `LayerAOutcome` (frozen)

Single immutable result type returned from the orchestrator (and optionally from each filter for internal tests).

| Field | Type | Meaning |
|--------|------|--------|
| `accept` | `bool` | If `False`, **stop**; emit skip logging + **`strategy_decision`** + per-filter **`layer_a_filter`** facts already emitted for steps run. |
| `reason_code` | `str` | Stable code for **final** outcome (extend **`ReasonCode`** / `StrEnum` in `core/reason_codes.py`). For chained gating, **first deny** wins for the final line; each step still emits its own **`layer_a_filter`** row. |
| `detail` | `str \| None` | Short operator/debug text (thresholds, token id fragment). |
| `metadata` | `dict[str, Any] \| None` | **JSON-serializable** values only. Merged across **interpretation** steps on success. |

### 5.1 Gating filters

- Set `accept=False` + **`reason_code`** + **`detail`** on deny.
- On pass: `accept=True`; **`metadata`** usually empty; optional diagnostic keys allowed if JSON-safe.

### 5.2 Interpretation filters

- On pass: `accept=True` and **must** set interpretation keys in **`metadata`**, e.g.:
  - `exit_qty_mode`: `"mirror_guru"` | `"full_position"` (v1).
  - `follower_position_qty`: `float` (optional, for logging when `full_position`).
- On fail (e.g. cannot read position for `full_exit`): `accept=False`, **explicit** `reason_code` — **no** fallback to mirror, **no** qty `0` passthrough.

### 5.3 Short-circuiting

- **Entry pipeline:** run in **fixed order** (§6). On **first gating deny**, stop; emit **`layer_a_filter`** for that filter; if **`significance_conviction.enabled`**, still run **`observe_buy`** after the chain when token passed and notional computable (§6, `03_significance_conviction_plan.md`).
- **Exit pipeline:** token (gating) → **`exit_filter`** (interpretation). Deny on interpretation failure stops; **no** mirror fallback.

### 5.4 Metadata merge

- Start with `metadata={}`.
- Each interpretation filter **updates** `metadata` with non-conflicting keys; **`exit_filter`** is the only interpretation filter in v1 — single writer for exit keys.
- **`CopyStrategy`** reads merged **`metadata`** after `accept=True` to choose exit sizing path.

---

## 6. Evaluation order (v1, locked)

All steps apply only to **`GuruTradeSignal`** after dedup on the bus.

| Step | Branch | Type | Notes |
|------|--------|------|------|
| 1 | both | Classify | `side` → `entry` \| `exit` (`_on_guru_trade`). |
| 2 | both | Gating | **Token allowlist** (from top-level `token_filter`); same semantics as **`GuruFollowEntryPolicy` / `GuruMirrorExitPolicy`** today. |
| 3 | exit | Interpretation | **`exit_filter`**: `mirror_guru` (default) or `full_exit` when enabled. |
| 4 | entry | Gating | **`static_amount`** (if enabled). |
| 5 | entry | Gating | **`significance_conviction`** (if enabled). |
| **Post** | entry, BUY only | State | **`SignificanceConvictionFilter.observe_buy`**: run only if **`significance_conviction.enabled`**; after full entry Layer A chain for this signal, if token gating passed and BUY notional computable, **append** — includes BUYs **denied** by static or significance (see `03_significance_conviction_plan.md`). |

**Branch-specific:** Steps 4–5 **only** for `branch=="entry"`. Step 3 **only** for `branch=="exit"`.

**Shared:** Step 2 for both.

---

## 7. Filter state ownership (v1)

| Filter | Stateful? | State | Persistence |
|--------|-----------|--------|-------------|
| Token allowlist | No | — | — |
| `exit_filter` | No | — | — |
| `static_amount` | No | — | — |
| `significance_conviction` | Yes | `deque` of last **N** BUY notionals (BUY-only history) | In-memory only v1; restart clears history (document in CONFIG_MODEL) |

**Conviction sizing** (`signal/sizing.py`) remains a **separate** deque — do not conflate with significance conviction history.

---

## 8. Config (v1) — backward-compatible token filter

### 8.1 Canonical operator config

- **`token_filter`** remains **required** at **strategy YAML top level** (current `load_strategy_settings` contract). **Do not** require moving it under `filters:` in v1.
- Optional **`filters:`** block adds exit + significance rules only.

### 8.2 Full v1 example

```yaml
guru_wallet_address: "0x..."

# Unchanged — canonical v1 location
token_filter:
  enabled: true
  allowlisted_token_ids: ["123..."]

copy_scale: 1.0
conviction_sizing_enabled: false
conviction_sizing_cap: 2.0
conviction_sizing_lookback_trades: 20
strategy_dedup_state_path: null

# New optional block — omit for legacy behavior (mirror exit, no significance gates)
filters:
  exit_filter:
    enabled: false
    exit_method: mirror_guru   # mirror_guru | full_exit
  significance_filter:
    static_amount:
      enabled: false
      amount_usd: 700.0
    significance_conviction:
      enabled: false
      lookback_trades: 20
      threshold_method: median   # v1: median only; loader rejects other values
```

**Loader defaults:** omitted `filters` → `exit_filter.enabled=false`, significance sub-blocks disabled — **parity** with today.

**Future (non-v1):** optional migration of `token_filter` under `filters:` may be documented later; not part of this iteration.

---

## 9. Reporting and observability (frozen)

Use **both**:

### 9.1 `strategy_decision` (existing)

- **Final** business outcome per guru signal: `accept`/`skip`, `branch`, **`reason_code`**, `correlation_id` aligned with **`source_trade_id`** (current `copy_strategy` pattern).
- Emitted **once** per signal after Layer A completes (same as today’s `_handle_branch` early returns).

### 9.2 `layer_a_filter` (new)

Emit **one record per filter step executed** (including passes), minimum fields:

| Field | Notes |
|--------|--------|
| `correlation_id` | Guru `source_trade_id` (same as existing facts) |
| `filter_name` | e.g. `token_allowlist`, `exit_interpretation`, `static_amount`, `significance_conviction` |
| `branch` | `entry` \| `exit` |
| `accept` | bool |
| `reason_code` | str (use `ReasonCode` / Layer A codes) |
| `detail` | optional str |
| `metadata` | JSON object; empty `{}` if none |

**Logging:** INFO on **final** skip/deny (single line with `correlation_id` + final `reason_code`); DEBUG for per-filter diagnostics optional. Avoid INFO spam per filter unless operator enables verbose mode (future).

---

## 10. Interaction with risk / execution

- Layer A **never** replaces **`ConfiguredRiskPolicy`**: **`price_ref × quantity`** caps, capital gate, deployment budget remain risk.
- **`full_exit`** produces a **follower-sized** qty from context; risk may still **clip** or deny.
- **`NautilusGuruExecutionPort`** unchanged in role; quantize still applies.

---

## 11. Persistence

- Significance conviction deque: **not** persisted v1.
- Guru dedup/watermark: unchanged (`runtime` YAML paths).

---

## 12. Migration / parity

1. Implement **`LayerAOutcome`** + orchestrator + **token filter** adapter (**behavior identical** to `entry.py` policies).
2. Wire **`CopyStrategy`** / **`BotSellValidateStrategy`** (subclass) to orchestrator — **no** behavioral regression when `filters` omitted.
3. Add gating + interpretation filters + reporting.
4. Remove or thin **`GuruFollowEntryPolicy` / `GuruMirrorExitPolicy`** only after tests prove parity (optional cleanup phase).

---

## 13. Testing strategy

- **Unit:** each filter + orchestrator (deny order, metadata merge, BUY observation after deny).
- **Integration:** `CopyStrategy` with synthetic msgbus + mock **`LayerAContext`**.
- **Regression:** existing `entry` / `copy_strategy` tests until migrated.

---

## 14. Implementation order (recommended)

1. **Core:** `LayerAOutcome`, `LayerAContext` protocol, `LayerAOrchestrator`, token gating adapter from top-level `token_filter` + **`layer_a_filter` / `strategy_decision`** emission hooks.
2. **`StaticAmountSignificanceFilter`** (stateless gating).
3. **`SignificanceConvictionFilter`** (BUY-only deque, median, post-chain `observe_buy` contract).
4. **`ExitInterpretationFilter`** + **`runtime`** context implementation + **`CopyStrategy`** exit sizing branch.
5. **`Docs/CONFIG_MODEL.md`**, scenario YAML examples, reason-code catalog.

---

## 15. Code layout (implementation)

| Path | Role |
|------|------|
| `src/tyrex_pm/signal/layer_a/types.py` | `LayerAOutcome`, `LayerAContext` |
| `src/tyrex_pm/signal/layer_a/orchestrator.py` | `LayerAOrchestrator` |
| `src/tyrex_pm/signal/layer_a/filters/token_allowlist.py` | Gating |
| `src/tyrex_pm/signal/layer_a/filters/static_amount.py` | Gating |
| `src/tyrex_pm/signal/layer_a/filters/significance_conviction.py` | Gating + `observe_buy` |
| `src/tyrex_pm/signal/layer_a/filters/exit_interpretation.py` | Interpretation |
| `src/tyrex_pm/signal/layer_a/notional.py` | Shared `notional_usd(sig)` helper |
| `src/tyrex_pm/runtime/layer_a_context.py` | Nautilus-backed `LayerAContext` |
| `src/tyrex_pm/config/loaders.py` | `filters` parse into `StrategySettings` |
| `src/tyrex_pm/runtime/guru_compose.py` | Inject context |
| `src/tyrex_pm/strategy/copy_strategy.py` | Orchestrator integration |

---

## 16. Remaining minor items (non-blocking)

- Exact **StrEnum** spellings for new codes — follow `core/reason_codes.py` naming review at implementation time.
- **`BotSellValidateStrategy`**: validate Layer A + validation harness interaction in one integration test.
- **SELL qty sign convention:** confirm positive quantity in `OrderIntent` matches **`NautilusGuruExecutionPort`** (code review during implementation).

---

## 17. Implementation-ready statement

This document is **implementation-ready** for v1 Layer A: rule taxonomy, result model, evaluation order, state, config, reporting, and boundaries are **frozen** except for §16 minor follow-ups during coding.
