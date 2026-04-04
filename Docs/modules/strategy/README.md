# Module: `tyrex_pm.strategy`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md) · [DEVELOPMENT](../../DEVELOPMENT.md)

## 1. General purpose of the strategy module

**Why strategy exists:** In NautilusTrader, a **Strategy** is the right place to **subscribe** to events, **orchestrate** policies, and decide **when** to act. Tyrex_PM uses that role explicitly: the strategy connects **bus messages** to **pure signal policies**, then to **risk** and **execution**.

**Why not venue code here:** `py-clob-client`, tick sizes, fee lookups, and HTTP errors are **execution concerns**. Putting them in the strategy would couple every future strategy to Polymarket and make shadow/live testing harder.

**Why not risk implementation here:** Limits, kill switches, and exposure caps are **centralized policy**. Embedding them in `CopyStrategy` would duplicate logic across strategies and blur logging (`copy_skip` vs `risk_denied`). The strategy **calls** `RiskPolicy.evaluate` and logs outcomes — it does **not** implement those rules.

---

## 2. Current example: guru follow (`CopyStrategy`)

**Class:** `src/tyrex_pm/strategy/copy_strategy.py`  
**Config:** `CopyStrategyConfig` (`token_filter_enabled`, `allowlisted_token_ids`, `execution_mode`, `copy_scale`) — mirrors strategy YAML `token_filter` via `guru_compose`.  
**Base:** `BaseComposableStrategy` (`src/tyrex_pm/strategy/base.py`) for shared Nautilus `Strategy` behavior / startup log.

### What signals it consumes

- Topic: **`GURU_TRADE_TOPIC`** (`tyrex_pm.guru.GuruTradeSignal`), same string as in `data/guru_monitor.py`.
- Payload: **`GuruTradeSignal`** (`core/types.py`) — includes `side`, `token_id`, sizes, price, `source_trade_id`, etc.

### Entry / exit

- **`side == "BUY"`** → `GuruFollowEntryPolicy.evaluate` (`signal/entry.py`).
- **`side == "SELL"`** → `GuruMirrorExitPolicy.evaluate`.
- Other sides → `copy_skip` with `ReasonCode.UNSUPPORTED_SIDE`.

### Sizing

- **`ProportionalSizingPolicy.size(signal)`** (`signal/sizing.py`) with strategy `copy_scale`.

### Risk

- Builds **`OrderIntent`** (correlation id, token, side, qty, `price_ref` from guru, reason from signal decision).
- Calls **`self._risk.evaluate(intent)`**. If rejected → `copy_skip` with `reason_code=risk_denied` and policy reason.

### Execution

- Calls **`self._execution.submit_intent(intent, mode=self._cfg.execution_mode)`**.
- Injected ports from **`guru_compose`**: **`NoOpExecutionPort`** (shadow); **`NautilusGuruExecutionPort`** (live + framework submit); **`PolymarketExecutionPolicy`** (live legacy py-clob).

### Shadow vs live behavior

| Step | Shadow | Live |
|------|--------|------|
| Policies + sizing | Same | Same |
| Risk | `ConfiguredRiskPolicy` | Same (readers injected; behavior varies by submit path — see **Architecture** / **current_state**) |
| Execution port | `NoOpExecutionPort` | **`NautilusGuruExecutionPort`** or **`PolymarketExecutionPolicy`** |
| Log after submit | `event=shadow_order_intent` | `event=live_order_intent` |

---

## 3. Example flows (practical)

### Guru BUY → happy path → shadow intent

1. `GuruMonitorActor` publishes `GuruTradeSignal` (BUY, token passes filter or filter off).
2. `CopyStrategy._on_guru_trade` → entry policy **accepts**.
3. Sizing returns **qty > 0**.
4. `OrderIntent` built with guru `price_ref`.
5. Risk **approves**.
6. `NoOpExecutionPort.submit_intent` runs (no HTTP).
7. Log: **`shadow_order_intent`** (when `execution_mode=="shadow"`).

### Guru SELL

Same pipeline using **exit** policy instead of entry; still BUY/SELL branches inside `_on_guru_trade`.

### Filtered-mode reject (`token_filter.enabled: true`)

1. Guru signal token **not** in `allowlisted_token_ids`.
2. Entry or exit policy returns **reject** (`SignalDecision.accept == False`).
3. Log: **`copy_skip`** with `not_allowlisted`.

With **`token_filter.enabled: false`**, this path does not apply; missing `token_id` on the signal is still rejected.

### Risk reject

1. Signal policies accept; qty > 0; `OrderIntent` built.
2. **`ConfiguredRiskPolicy.evaluate`** returns `(False, reason)` — e.g. kill switch, over max notional.
3. Log: **`copy_skip`** with **`risk_denied`** and detail.

### Shadow intent emission

Shadow is steps 1–7 in the BUY flow with **`execution_mode: shadow`** — execution port is noop; **no CLOB**.

### Live intent submission

1. Same through risk approve.
2. **`execution_mode: live`** → **`submit_intent`** on **`NautilusGuruExecutionPort`** or **`PolymarketExecutionPolicy`** (per runtime flags).
3. Strategy logs **`live_order_intent`**; execution logs framework **`LIVE_ORDER_SUBMIT`** / **`LIVE_ORDER_ERROR`** or legacy py-clob messages (see [OPERATIONS.md](../../OPERATIONS.md)).

---

## A–F. Standard module sections

### A. Role (summary)

Nautilus **Strategy** implementations: orchestrate signal policies, risk, and execution for copy trading.

### B. Boundaries

**Belongs:** Message bus subscription, orchestration, structured logging, `OrderIntent` assembly.

**Does not:** HTTP Data API, CLOB orders, risk limit definitions.

### C. Internal structure

| File | Role |
|------|------|
| `base.py` | `BaseComposableStrategy` — minimal shared behavior. |
| `copy_strategy.py` | **`CopyStrategy`**, **`CopyStrategyConfig`**. |
| `logutil.py` | Shared log line helpers. |

### D. Main interactions

- **data:** consumes guru topic.
- **signal / risk / execution:** as above.
- **runtime:** registers strategy on `TradingNode`.

### E. Status

**CopyStrategy** is the v1 production path for guru follow.

### F. Extension guidance

- New strategies: new `Strategy` subclass + policies; **reuse** `RiskPolicy` / `ExecutionPort` injection pattern.
- Keep **`OrderIntent`** as the handoff to execution unless you introduce a versioned successor type.
- See [Architecture.md](../../Architecture.md) §G for guru finder and replay extensions.
