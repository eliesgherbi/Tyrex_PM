# Developer guide — Tyrex_PM

**Architecture:** [Architecture.md](Architecture.md) · **Doc index:** [README.md](README.md) · **Module map:** [modules/README.md](modules/README.md) · **Config fields:** [CONFIG_MODEL.md](CONFIG_MODEL.md)

---

## 1. Mental model

Tyrex_PM is a **guru-following** stack on **NautilusTrader**: ingest publishes **`GuruTradeSignal`** on the message bus; **`CopyStrategy`** turns signals into **`OrderIntent`** after **policy + sizing**, then **risk** (may clip/bump per-order deploy), then **execution**.

Think in **four ownership layers** (do not collapse them):

1. **`signal/`** — *Should we take this signal? How many shares at policy baseline (scale / optional conviction)?* Pure functions / small classes; **no** bus, HTTP, or `Cache`. **Not** “too small to trade” in USD — that is **`risk/`**.
2. **`risk/`** — *Is this intent allowed given per-order min/max deploy (**deny** vs **cap**), token/portfolio caps, capital gate, Phase B B2–B4?* **`ConfiguredRiskPolicy`**; readers injected from **`runtime/guru_compose`** — **never** import `Cache` inside policy implementations; use injected readers.
3. **`execution/`** — *How do we express this **approved** intent at the venue?* Ports: **`NoOpExecutionPort`**, **`NautilusGuruExecutionPort`** (live). **Book C3** (guard / depth / limit timeout) + internal grid quantize live in **`nautilus_guru_exec`**.
4. **`data/`** + **`runtime/`** — Ingest (poll / RTDS / gap-fill), compose, wiring, **`state_readers`**, dynamic instruments.

**Source-of-truth for market state (live framework path):** Nautilus **`Cache`** / **`Portfolio`** as fed by the Polymarket adapter — not a duplicate ledger inside Tyrex. Restart semantics: **`load_state=False`** on the node; see **`Implementation/phase_a_closure.md`**.

---

## 2. End-to-end flow (current)

```
GuruMonitorActor / GuruStreamActor (+ pipeline, dedup)
  → MessageBus GURU_TRADE_TOPIC
  → CopyStrategy
       → entry/exit policies (signal)
       → sizing + record_accepted_entry_size (signal; C2 conviction optional)
       → OrderIntent
       → RiskPolicy.evaluate (per-order clip/bump + caps)
       → ExecutionPort.submit_intent
            → NautilusGuruExecutionPort (C3 optional pre-submit + limit + timer)
```

**Thin strategy rule:** `CopyStrategy` orchestrates and logs; it does **not** implement risk rules, venue normalization, or order-book logic. The only **execution** touch is **`on_order_event`** forwarding to the port for **C3 timer cleanup** — no order interpretation beyond that.

---

## 3. Where to add behavior

| You want to… Put it in… |
|------|------------|
| Change token allow / BUY vs SELL accept | `signal/entry.py` |
| Change follower size vs guru (flat scale, conviction, rolling avg) | `signal/sizing.py` |
| Economic “too small / too large” per-order USD deploy | `risk/configured.py` — `min_*` / `max_*` + policies |
| Caps, portfolio exposure, concurrent orders, reserve | `risk/configured.py` (+ Phase B docs) |
| Tick rounding, slippage guard vs book, depth clip, limit timeout | `execution/` helpers + **`NautilusGuruExecutionPort`** (C3); **not** in strategy |
| New ingest source (still guru trades) | `data/` — publish same **`GuruTradeSignal`** / topic |
| Wire readers / choose execution port | `runtime/guru_compose.py` (and loaders) |
| Emit structured run facts (risk/strategy/execution hooks) | `reporting/` + emit sites in `risk/configured.py`, `copy_strategy.py`, etc.; **`Implementation/reporting_fact_model.md`** |
| New YAML knobs | `config/loaders.py` + **CONFIG_MODEL.md** |

---

## 4. Anti-patterns (explicit)

- **Don’t** put **`Cache` / order-book / venue tick** logic in **`CopyStrategy`** (C3 is execution-owned).
- **Don’t** move **Phase B** exposure or kill-switch semantics into **`signal/`**.
- **Don’t** treat **venue rejects** as the primary control — pre-trade normalize/C3 and risk gates are first-class; rejects are still signals to monitor.
- **Don’t** add operator-facing **venue alignment** or sizing policy in execution — risk owns size; execution only resolves, applies instrument grid fit, submits, and reports lifecycle.
- **Don’t** implement a parallel **guru submit** path outside **`NautilusGuruExecutionPort`** for live runs.

---

## 5. Repository layout (`src/tyrex_pm/`)

| Package | Role |
|---------|------|
| `core/` | `GuruTradeSignal`, `OrderIntent`, **`ReasonCode`**, shared helpers |
| `config/` | `StrategySettings`, `RiskSettings`, `RuntimeSettings` + loaders |
| `data/` | Data API client, **RTDS** stream actor, poll actor, parse/dedup, pipeline |
| `signal/` | Entry/exit, sizing (C2 conviction optional) |
| `risk/` | `RiskPolicy`, **`ConfiguredRiskPolicy`** |
| `execution/` | Ports, **C3** helpers (`c3_*.py`), `nautilus_guru_exec.py` |
| `strategy/` | **`CopyStrategy`**, `BaseComposableStrategy` |
| `runtime/` | **`build_guru_trading_node`**, readers, dynamic instruments, clob factory, **CLOB collateral parsing** (`clob_collateral_money.py`), **Nautilus cash extract** |
| `reporting/` | **`RunContext`**, JSONL sink, schema, **`summarize`**, order-event mapping (optional; `reporting_enabled`) |

---

## 6. Config flow

1. **`scripts/run_guru.py`** loads `.env`, then three YAML paths → **`load_strategy_settings`**, **`load_risk_settings`**, **`load_runtime_settings`**.
2. **`build_guru_trading_node(strategy, risk, runtime)`** constructs the node, injects risk readers, sets **`CopyStrategyConfig`** from strategy + runtime (including **C2** fields), chooses **`ExecutionPort`** from **`execution_mode`** (shadow vs live Nautilus).

**Secrets:** only **`.env`** / environment — never strategy or runtime YAML.

---

## 7. Tests & debugging

**Run:** `pytest tests/ -q` from repo root after `pip install -e ".[dev]"`.

**High-signal suites:**

| Area | Tests |
|------|--------|
| Policy / C2 | `tests/unit/test_c2_capital_allocation.py`, `tests/unit/test_copy_strategy_shadow.py` |
| C3 execution | `tests/unit/test_c3_execution.py`, `tests/test_nautilus_guru_exec.py` |
| Risk / Phase B | `tests/unit/test_configured_risk.py`, `tests/test_phase_b_*.py` |
| Config | `tests/test_split_config_loaders.py` |
| Compose | `tests/test_guru_compose_build.py` |
| Copy strategy architecture guard | `tests/test_copy_strategy_architecture.py` |

**Debugging order:**

1. **`guru_signal_emitted`** present? (ingest / wallet / mode)  
2. **`copy_skip`** reason? (token, zero qty, **risk_denied**, …)  
3. **`live_order_intent`** / **`shadow_order_intent`** then **`LIVE_ORDER_SUBMIT`** or **exec skip** (`exec_*` for C3)?  
4. For risk: **`tyrex_risk_ops`** alongside **`copy_skip`**

Log files: **`logs/<execution_mode>/run_tyrex.log`** vs **`run_nautilus.log`** — see [logging_system_guide.md](logging_system_guide.md).

---

## 8. Path truth table (for contributors)

| Mode / path | Notes |
|-------------|--------|
| **`guru_ingest_mode`** | `poll_only` · `rtds_shadow` (poll publishes, stream compares) · **`rtds_primary`** (stream publishes when healthy). |
| **`execution_mode`** | `shadow` → **`NoOpExecutionPort`**; `live` → **`NautilusGuruExecutionPort`** (**C3** applies on live). |

---

## 9. Further reading

- **Implementation hub:** [Implementation/current_state.md](Implementation/current_state.md)  
- **C3 design / deferred items:** [Implementation/plan_C3_Execution-Quality.md](Implementation/plan_C3_Execution-Quality.md)  
- **Phase B normative:** [Implementation/Phase_B_planing.md](Implementation/Phase_B_planing.md)
