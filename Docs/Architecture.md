# Tyrex_PM — architecture overview

Grounded in the current codebase (`src/tyrex_pm/`). For day-to-day runbooks, see [OPERATIONS.md](OPERATIONS.md). For YAML field tables, see [CONFIG_MODEL.md](CONFIG_MODEL.md).

**Per-module detail:** [modules/README.md](modules/README.md).

---

## A. Project purpose

**What Tyrex_PM is:** a Python package for **Polymarket** automation, organized around **NautilusTrader** patterns (actors, strategies, message bus) while keeping **venue I/O** and **risk** in explicit layers.

**Current v1 scope:** a **guru-follow copy** path — poll a wallet’s trades from the public Data API, turn them into internal signals, size them, run **fail-closed risk**, and either **log-only shadow** or **live** execution (legacy py-clob and/or **Nautilus framework submit**).

**Implemented now:**

- Incremental **Data API** polling (`GET /activity`, `type=TRADE`) + watermark + optional dedup + `GuruTradeSignal` publication (`data/guru_monitor.py`, `data/guru_watermark.py`).
- Entry / exit / sizing policies (`signal/`).
- **`CopyStrategy`** (`strategy/copy_strategy.py`) — thin orchestration; **no** direct `Cache` / `Portfolio` use.
- Typed YAML: strategy / risk / runtime (`config/loaders.py`).
- **`ConfiguredRiskPolicy`** — static limits + optional **framework-backed** pending (open orders, **leaves qty**), **filled** exposure (`Portfolio.net_exposure`), optional **capital gate** (account + py-clob allowance snapshots).
- **`execution/`** — `NoOpExecutionPort`, **`PolymarketExecutionPolicy`** (py-clob), **`NautilusGuruExecutionPort`** (framework `submit_order`).
- **`runtime/state_readers.py`** — canonical read boundary; injected into risk from `guru_compose`.
- **Dynamic instruments / zero-bootstrap** — `guru_instrument_dynamic.py`, optional `guru_cache_warmup.py` (see `Implementation/step_5_runtime_integration.md`).
- **`scripts/run_guru.py` + `guru_compose.py`** — `TradingNode` with **empty** or **Polymarket live** clients per runtime flags.

**Intentionally deferred / out of scope:**

- Guru **discovery / ranking / analytics** (separate).
- **Phase C** follow-policy knobs (cooldowns, per-cycle caps, suppression) — `road_map.md`.
- Rich **reporting** / **indicators** (stubs).

**Maintainer hub:** [`Implementation/current_state.md`](Implementation/current_state.md) · **Phase A:** [`Implementation/phase_a_closure.md`](Implementation/phase_a_closure.md).

---

## B. Architectural principles

| Principle | What it means here |
|-----------|-------------------|
| **Modularity** | Packages under `src/tyrex_pm/*` with small public surfaces (`__init__.py` exports where useful). |
| **Separation of concerns** | Strategy orchestrates; `signal/` is pure policy; `data/` owns external read I/O; `risk/` and `execution/` own gates and venue translation. |
| **Shadow → live continuity** | Same `CopyStrategy`, same `OrderIntent`, same composition; only **`execution_mode`** and the **`ExecutionPort`** implementation change. |
| **Strategy / risk / execution** | Strategy calls `RiskPolicy.evaluate` then `ExecutionPort.submit_intent` — it does **not** embed limit formulas, kill-switch rules, or `py-clob` calls. |
| **Secrets vs config** | `.env` (or env vars) for keys; YAML for non-secrets — see [CONFIG_MODEL.md](CONFIG_MODEL.md). |
| **Data / strategy / runtime split** | **Data** publishes facts; **strategy** decides; **runtime** wires Nautilus + policies + config loaders. |
| **Fail-closed risk** | Missing price, over limit, or kill switch → reject with stable `ReasonCode` strings (`core/reason_codes.py`). |

---

## C. High-level module map

| Module path | Role |
|-------------|------|
| **core** | Shared types (`GuruTradeSignal`, `OrderIntent`), `ReasonCode`, legacy app YAML helpers, logging bits. |
| **config** | Typed settings dataclasses + YAML loaders for **strategy / risk / runtime** (no secrets). |
| **data** | Market helpers (allowlist, resolution, book check), Data API HTTP client, guru parse/dedup, **`GuruMonitorActor`**. |
| **signal** | Reusable decision + sizing logic **without** Nautilus or HTTP. |
| **risk** | `RiskPolicy`, `ConfiguredRiskPolicy` (readers injected from runtime). |
| **execution** | `ExecutionPort`, `NoOpExecutionPort`, `PolymarketExecutionPolicy`, **`NautilusGuruExecutionPort`**. |
| **strategy** | `BaseComposableStrategy`, **`CopyStrategy`**. |
| **runtime** | `guru_compose`, **`state_readers`**, **`guru_instrument_dynamic`**, `polymarket_nautilus_env`, `clob_factory`, `live_stub`. |
| **reporting** | Placeholder for future run reports. |

`indicator/` exists as a stub; see [modules/indicator/README.md](modules/indicator/README.md).

---

## D. Module interaction diagram

```mermaid
flowchart TB
  subgraph Config
    YAML[Strategy / Risk / Runtime YAML]
    Loaders[config.loaders]
  end

  subgraph DataLayer[data]
    Actor[GuruMonitorActor]
    API[PolymarketDataApiClient]
    Actor --> API
  end

  subgraph Bus[Nautilus MessageBus]
    Topic["topic: tyrex_pm.guru.GuruTradeSignal"]
  end

  subgraph StrategyLayer[strategy]
    Copy[CopyStrategy]
    Entry[entry policies]
    Exit[exit policies]
    Size[sizing policy]
    Copy --> Entry
    Copy --> Exit
    Copy --> Size
  end

  subgraph RiskExec[risk + execution]
    Risk[ConfiguredRiskPolicy]
    Readers[state_readers inject]
    XShad[NoOpExecutionPort]
    XLive[PolymarketExecutionPolicy]
    XNau[NautilusGuruExecutionPort]
    CLOB[py-clob CLOB]
    NT[Nautilus ExecEngine / Cache]
    XLive --> CLOB
    XNau --> NT
    Readers -.-> Risk
  end

  YAML --> Loaders
  Loaders --> Runtime[guru_compose]
  Runtime --> TradingNode[TradingNode]
  TradingNode --> Actor
  TradingNode --> Copy
  Actor -->|publish| Topic
  Topic -->|subscribe| Copy
  Copy -->|evaluate| Risk
  Copy -->|submit_intent| XShad
  Copy -->|submit_intent| XLive
  Copy -->|submit_intent| XNau
```

**Live:** Shadow uses `NoOpExecutionPort`. **Legacy live** uses `PolymarketExecutionPolicy` → py-clob; **`note_fill_assumption`** updates `_token_open` for the token cap. **Framework live** (`polymarket_framework_submit`) uses `NautilusGuruExecutionPort` → `submit_order`; pending cap uses **`Cache` open orders** (leaves qty); token cap adds **filled** exposure from `Portfolio.net_exposure` when the position reader is wired (`note_fill_assumption` is a no-op for pending).

**ASCII (same idea):**

```
  [YAML] -> loaders -> guru_compose -> TradingNode (+ optional/state readers -> risk)
                              |-> GuruMonitorActor --(bus)--> CopyStrategy
                              |                               |-> RiskPolicy
                              |                               |-> ExecutionPort (NoOp / py-clob / Nautilus)
                              +-> Path A: Polymarket DATA+EXEC clients on node
```

---

## E. Runtime flow (`scripts/run_guru.py`)

1. **CLI** parses `--strategy-conf`, `--risk-conf`, `--live-conf`.
2. **Env:** `python-dotenv` loads repo `.env` or `TYREX_PM_DOTENV` (does not replace shell overrides).
3. **Config:** `load_strategy_settings`, `load_risk_settings`, `load_runtime_settings` validate and return dataclasses.
4. **Composition:** `build_guru_trading_node(strategy, risk, runtime)`:
   - Builds `TradingNodeConfig` (`trader_id`, `LoggingConfig`, **`load_state=False`, `save_state=False`**; data/exec clients **empty** or **Polymarket live** when `polymarket_nautilus_live` + `execution_mode: live`).
   - Instantiates `GuruMonitorActor` (wallet, poll interval, dedup path, Data API URL).
   - Instantiates `CopyStrategy` with `token_filter` + `copy_scale` + `execution_mode` from strategy YAML.
   - Builds **state readers** (`NautilusExecutionStateReader`, `NautilusAccountSnapshotProvider`, optional `ClobAllowanceStateProvider`, optional `NautilusPositionStateReader`) and injects them into **`ConfiguredRiskPolicy`**.
   - Injects execution port: **`NoOpExecutionPort`** (shadow), **`NautilusGuruExecutionPort`** (live + framework submit), or **`PolymarketExecutionPolicy`** (live legacy, with `on_submit_ok=note_fill_assumption`).
   - Registers **actor** and **strategy** on the trader **before** `build()`.
5. **Phase A line:** For live framework mode, `run_guru.py` may print a short **phase_a** reminder; see `phase_a_closure.md`.
6. **Lifecycle:** `node.build()` then `node.run()` — Nautilus starts clocks; actor `on_start` runs first poll + timer; strategy subscribes to guru topic.
7. **Signal flow:** each poll fetches **recent** `TRADE` activity after the stored watermark; rows newer than the watermark emit `GuruTradeSignal` (dedup as safety net) on the bus → `CopyStrategy._on_guru_trade` → …
8. **Logs:** structured `event=` lines (`guru_signal_emitted`, `guru_poll_error`, `copy_skip`, `shadow_order_intent` / `live_order_intent`, framework `LIVE_ORDER_SUBMIT` / guru `ReasonCode` from `nautilus_guru_exec`). Operators: [OPERATIONS.md](OPERATIONS.md).

---

## F. Shadow vs live

| Aspect | Shadow | Live (legacy py-clob) | Live (Path A + framework submit) |
|--------|--------|----------------------|----------------------------------|
| **`ExecutionPort`** | `NoOpExecutionPort` | `PolymarketExecutionPolicy` | `NautilusGuruExecutionPort` |
| **Node clients** | Typically empty | May be empty or Nautilus (mixed ops) | **Polymarket DATA + EXEC** registered |
| **Pending token cap** | N/A / same YAML | **`_token_open`** after HTTP OK | **`Cache` orders**, **leaves × price** |
| **Filled token cap** | N/A | Not framework-based | **`net_exposure`** (adapter-dependent) |
| **Capital gate** | Same YAML (allowance provider **None** in shadow) | Optional | Optional |
| **Secrets** | — | `.env` + L2 | `.env` + L2 |

**Why:** operators validate in **shadow** without CLOB; live mode selects **legacy** vs **framework** submit via **`polymarket_framework_submit`** (requires **`polymarket_nautilus_live`**). Strategy code path unchanged; only ports and reader-derived risk behavior differ.

---

## G. Limitations and extension points

| Area | Current state | Notes |
|------|---------------|--------|
| **Guru input** | Single wallet, **`/activity` polling** + watermark | Not full `/trades` history crawler. |
| **Risk / exposure** | Framework path: **pending + filled + capital gate** (optional); legacy path: **`_token_open`** | **Filled** and **events** depend on **Nautilus + Polymarket adapter** updating `Portfolio` / `Cache`. |
| **Execution** | Sync submit (py-clob or framework) | Queue/cancel/replace — future `ExecutionPort` work. |
| **Restart** | **`load_state=False`** in `guru_compose` | Post-restart truth = **venue + adapter** + optional Tyrex warmup; see `phase_a_closure.md`. |
| **Phase B / C** | **Deferred** roadmap work | Cooldowns, reserves, venue normalize — `road_map.md`. |
| **New strategies** | `CopyStrategy` | Reuse injected `RiskPolicy` / `ExecutionPort` pattern. |

---

## Where to read next

1. **[Implementation/current_state.md](Implementation/current_state.md)** — migration / status hub.
2. **[modules/README.md](modules/README.md)** — per-module docs.
3. **[OPERATIONS.md](OPERATIONS.md)** — runbook, modes, log semantics.
4. **[CONFIG_MODEL.md](CONFIG_MODEL.md)** — YAML fields.
5. **[DEVELOPMENT.md](DEVELOPMENT.md)** — tests, conventions.
