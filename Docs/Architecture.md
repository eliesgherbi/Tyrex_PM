# Tyrex_PM — architecture overview

Grounded in the current codebase (`src/tyrex_pm/`). **Documentation hub:** [README.md](README.md). For operators: [OPERATIONS.md](OPERATIONS.md). For YAML: [CONFIG_MODEL.md](CONFIG_MODEL.md). For contributors: [developer_guide.md](developer_guide.md).

**Per-module detail:** [modules/README.md](modules/README.md).

---

## A. Project purpose

**What Tyrex_PM is:** a Python package for **Polymarket** automation, organized around **NautilusTrader** patterns (actors, strategies, message bus) while keeping **venue I/O** and **risk** in explicit layers.

**Current v1 scope:** a **guru-follow copy** path — ingest a wallet’s trades from **Polymarket RTDS** (recommended: **`guru_ingest_mode: rtds_primary`**) and/or incremental **Data API** polling as shadow, validation, or fallback, turn them into internal signals, size them, run **fail-closed risk**, and either **log-only shadow** or **live** execution via **Nautilus** (`submit_order` on **`NautilusGuruExecutionPort`**).

**Implemented now:**

- **Guru ingestion (RTDS + poll):** **`GuruStreamActor`** — RTDS WebSocket (`activity` / `trades`), `proxyWallet` filter, shared dedup/watermark with poll, reconnect + liveness + optional REST **gap-fill** (`data/guru_stream_actor.py`, `guru_rtds_ws.py`, `guru_rtds_parse.py`, `guru_gap_fill.py`). **`GuruIngestRuntimeState`** selects publish path: `poll_only` | `rtds_shadow` | `rtds_primary` (`data/guru_ingest_state.py`).
- Incremental **Data API** polling (`GET /activity`, `type=TRADE`) + watermark + optional dedup + `GuruTradeSignal` publication (**`GuruMonitorActor`**, `data/guru_monitor.py`, `data/guru_watermark.py`).
- Shared **`GuruSignalPipeline`** for dedup + bus publish + structured **`guru_signal_emitted`** logging (`data/guru_ingest_pipeline.py`).
- Entry / exit / sizing / optional **conviction** weighting (`signal/` — `entry`, `sizing`; per-order min/max USD is **risk** only).
- **`CopyStrategy`** (`strategy/copy_strategy.py`) — thin orchestration; **no** direct `Cache` / `Portfolio` / order-book use; forwards **`on_order_event`** to the execution port for **limit-order timeout** cleanup when enabled.
- Typed YAML: strategy / risk / runtime (`config/loaders.py`).
- **`ConfiguredRiskPolicy`** — **deployment-budget** caps (pending ``leaves ×`` limit + filled ``abs(qty) × avg_px_open`` via **`NautilusDeploymentBudget`**), optional **capital gate** (account + py-clob allowance snapshots). **Not** mark / ``net_exposure`` for caps.
- **`execution/`** — `NoOpExecutionPort`, **`NautilusGuruExecutionPort`** (live `submit_order`). Optional book hooks + mandatory instrument grid quantize in **`nautilus_guru_exec`** — see **`CONFIG_MODEL.md`** (`execution_*` keys; no operator venue-alignment YAML).
- **`runtime/state_readers.py`** — canonical read boundary; injected into risk from `guru_compose`.
- **Dynamic instruments / zero-bootstrap** — `guru_instrument_dynamic.py`, optional `guru_cache_warmup.py`.
- **`scripts/run_guru.py` + `guru_compose.py`** — `TradingNode` with **empty** clients (shadow) or **Polymarket live** data + exec (live).

**Intentionally deferred / out of scope:**

- Guru **discovery / ranking / analytics** (separate product surface).
- Rich **analytics indicators** / dashboards (beyond post-run **`summarize`** on **`facts.jsonl`**).
- Broader pacing, TWAP, analytics — **not** in core guru-follow scope unless added in code; see **`Implementation/road_map.md`** (archived backlog).

**Structured run reporting (optional):** when **`reporting_enabled`** in runtime YAML, each run writes **`var/reporting/runs/<run_id>/`** — **`Docs/reporting_fact_model.md`**, **`Docs/OPERATIONS.md`** § Structured reporting.

**Maintainer hub:** [`Implementation/current_state.md`](Implementation/current_state.md) · **End-to-end trace:** [`Implementation/end_to_end_review_logic.md`](Implementation/end_to_end_review_logic.md).

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
| **data** | Market helpers (allowlist, resolution, book check), Data API HTTP client, guru parse/dedup, **`GuruMonitorActor`**, **`GuruStreamActor`** (RTDS), gap-fill, ingest pipeline. |
| **signal** | Reusable decision + sizing logic **without** Nautilus or HTTP. |
| **risk** | `RiskPolicy`, `ConfiguredRiskPolicy` (readers injected from runtime). |
| **execution** | `ExecutionPort`, `NoOpExecutionPort`, **`NautilusGuruExecutionPort`**. |
| **strategy** | `BaseComposableStrategy`, **`CopyStrategy`**. |
| **runtime** | `guru_compose`, **`state_readers`**, **`deployment_budget`**, **`guru_instrument_dynamic`**, `polymarket_nautilus_env`, `clob_factory`, `live_stub`. |
| **reporting** | **Run observability:** `facts.jsonl`, manifest, optional SQLite + `summarize` (**`Docs/modules/reporting/README.md`**, **`Docs/reporting_fact_model.md`**). |

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
    Stream[GuruStreamActor]
    API[PolymarketDataApiClient]
    Actor --> API
    Stream -.->|optional WS| RTDS[RTDS activity/trades]
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
    XNau[NautilusGuruExecutionPort]
    NT[Nautilus ExecEngine / Cache]
    XNau --> NT
    Readers -.-> Risk
  end

  YAML --> Loaders
  Loaders --> Runtime[guru_compose]
  Runtime --> TradingNode[TradingNode]
  TradingNode --> Actor
  TradingNode --> Stream
  TradingNode --> Copy
  Actor -->|publish| Topic
  Stream -.->|rtds_primary: publish| Topic
  Topic -->|subscribe| Copy
  Copy -->|evaluate| Risk
  Copy -->|submit_intent| XShad
  Copy -->|submit_intent| XNau
```

**Live:** Shadow uses `NoOpExecutionPort`. **Live** uses `NautilusGuruExecutionPort` → `submit_order`; pending cap uses **`Cache` open orders** (leaves qty); token cap adds **filled** exposure from `Portfolio.net_exposure` via the position reader.

**ASCII (same idea):**

```
  [YAML] -> loaders -> guru_compose -> TradingNode (+ optional/state readers -> risk)
                              |-> GuruMonitorActor --(bus)--> CopyStrategy
                              |-> GuruStreamActor (RTDS; rtds_primary publish / rtds_shadow compare)
                              |                               |-> RiskPolicy
                              |                               |-> ExecutionPort (NoOp / Nautilus)
                              +-> Path A: Polymarket DATA+EXEC clients on node
```

---

## E. Runtime flow (`scripts/run_guru.py`)

1. **CLI** parses `--strategy-conf`, `--risk-conf`, `--live-conf`.
2. **Env:** `python-dotenv` loads repo `.env` or `TYREX_PM_DOTENV` (does not replace shell overrides).
3. **Config:** `load_strategy_settings`, `load_risk_settings`, `load_runtime_settings` validate and return dataclasses.
4. **Composition:** `build_guru_trading_node(strategy, risk, runtime)`:
   - Builds `TradingNodeConfig` (`trader_id`, `LoggingConfig`, **`load_state=False`, `save_state=False`**; data/exec clients **empty** (shadow) or **Polymarket live** (`execution_mode: live`).
   - Instantiates `GuruMonitorActor` (wallet, poll interval, dedup path, Data API URL) — always registered; poll **publishes** when `guru_ingest_mode` is `poll_only` or `rtds_shadow`, and in `rtds_primary` **only during fallback** when configured.
   - If `guru_ingest_mode` is `rtds_shadow` or `rtds_primary`, registers **`GuruStreamActor`** (RTDS URL, shared dedup/watermark, ingest state). **Primary:** stream publishes when not in fallback; **shadow:** stream logs `guru_stream_would_emit` only.
   - Instantiates `CopyStrategy` with strategy YAML (`token_filter`, `copy_scale`, optional conviction fields) and **`execution_mode`** from runtime YAML.
   - Builds **state readers** and injects **`NautilusDeploymentBudget`** + execution/account/allowance readers into **`ConfiguredRiskPolicy`**. Wires **`NautilusPositionStateReader`** into **`CopyStrategy`** for optional **reporting** only (marked exposure facts), not for risk caps.
   - Injects execution port: **`NoOpExecutionPort`** (shadow) or **`NautilusGuruExecutionPort`** (live).
   - Registers **actor** and **strategy** on the trader **before** `build()`.
5. **Lifecycle:** `node.build()` then `node.run()` — Nautilus starts clocks; actor `on_start` runs first poll + timer; strategy subscribes to guru topic.
6. **Signal flow:** **RTDS path** (when enabled): stream parses trade payloads, matches `proxyWallet`, emits **`guru_signal_emitted`** with `source=rtds` on publish. **Poll path:** fetches **`GET /activity`** `TRADE` rows after watermark; emits with `source=poll`. **Gap-fill** may emit with `source=gap_fill`. Shared dedup prevents duplicate `correlation_id` / `source_trade_id`. Bus → **`CopyStrategy._on_guru_trade`** → entry/exit → sizing (optional conviction) → **`OrderIntent`** → **`risk.evaluate`** (per-order clip/bump + caps) → **`ExecutionPort.submit_intent`** (optional book hooks on **`NautilusGuruExecutionPort`**).
7. **Logs:** structured `event=` lines (`guru_signal_emitted` with `source=`, `guru_stream_would_emit` in shadow, RTDS/fallback/gap-fill events, `guru_poll_error`, `copy_skip`, `shadow_order_intent` / `live_order_intent`, framework `LIVE_ORDER_SUBMIT` / guru `ReasonCode` from `nautilus_guru_exec`). Operators: [OPERATIONS.md](OPERATIONS.md).

---

## F. Shadow vs live

| Aspect | Shadow | Live |
|--------|--------|------|
| **`ExecutionPort`** | `NoOpExecutionPort` | `NautilusGuruExecutionPort` |
| **Node clients** | Empty | **Polymarket DATA + EXEC** |
| **Pending token cap** | N/A | **`Cache` orders**, **leaves × price** |
| **Filled deployment (token/portfolio caps)** | N/A | **`avg_px_open × abs(qty)`** on open positions (cost basis; adapter must populate positions) |
| **Capital gate** | Allowance provider **None** | Optional |
| **Secrets** | — | `.env` + L2 |

**Why:** operators validate in **shadow** without venue clients; **live** is always the Nautilus framework path. Strategy code path unchanged; ports and reader wiring differ by mode.

---

## G. Limitations and extension points

| Area | Current state | Notes |
|------|---------------|--------|
| **Guru input** | Single wallet; **recommended** RTDS **`rtds_primary`** + poll fallback/shadow; **`poll_only`** available | Not full `/trades` history crawler; RTDS is unfiltered stream (client-side wallet filter). |
| **Risk / exposure** | **Live:** pending **Cache** open orders (leaves) + filled **`Portfolio`** + optional **capital gate** | **Filled** and **events** depend on **Nautilus + Polymarket adapter** updating `Portfolio` / `Cache`. |
| **Execution** | **Live:** Nautilus `submit_order` via **`NautilusGuruExecutionPort`**; book hooks optional | Limit lifecycle / timeout implemented on the guru Nautilus path. |
| **Restart** | **`load_state=False`** in `guru_compose` | Post-restart truth = **venue + adapter** + optional Tyrex warmup. |
| **Follow extras** | Pacing, TWAP, richer suppression | **Not** shipped unless implemented in code — see **`Implementation/road_map.md`** (archived backlog). |
| **New strategies** | `CopyStrategy` | Reuse injected `RiskPolicy` / `ExecutionPort` pattern. |

---

## Where to read next

1. **[Implementation/current_state.md](Implementation/current_state.md)** — migration / status hub.
2. **[modules/README.md](modules/README.md)** — per-module docs.
3. **[OPERATIONS.md](OPERATIONS.md)** — runbook, modes, log semantics.
4. **[CONFIG_MODEL.md](CONFIG_MODEL.md)** — YAML fields.
5. **[developer_guide.md](developer_guide.md)** — boundaries, tests, shadow vs live.
6. **Module guides:** [modules/README.md](modules/README.md) — `DEVELOPER.md` per mature package.
