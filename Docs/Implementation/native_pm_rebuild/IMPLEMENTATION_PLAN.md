# Tyrex_PM — native Polymarket rebuild: implementation plan (parity-first)

**Approved architecture (unchanged):** [ARCHITECTURE.md](ARCHITECTURE.md) · **Event contracts:** [EVENT_CATALOG.md](EVENT_CATALOG.md) · **Scope reference:** [COPY_STRATEGY_SCOPE.md](COPY_STRATEGY_SCOPE.md)

This document is the **single** implementation track: **copy-strategy parity first**, then other strategy families use the same codebase. There is **no** alternate architecture, **no** version branch of the design, and **no** optional “maybe” blocks inside the parity definition.

---

## 1. Final executive target

Build one **Polymarket-native** runtime:

- **Adapters:** Market WS, User WS, Gamma, Data API, CLOB (submit/cancel + heartbeat).
- **Async typed event bus** (`core/bus.py`) carrying events from [EVENT_CATALOG.md](EVENT_CATALOG.md).
- **Explicit stores:** market, wallet, order, strategy (`state/`).
- **Single-writer OMS per wallet** (`execution/oms.py`): only component that calls CLOB for that wallet.
- **Thin guru-follow strategy** (`strategies/guru_follow/`) emitting intents only.
- **Composable `RiskEngine`** (`risk/engine.py` + policy modules): fail-closed, reason-coded, deployment- and inventory-aware.
- **Reporting on the main path** (`reporting/`): every run produces joinable **facts** + **manifest**; operators can diagnose without log archaeology.

Nautilus is **out of scope** — not supported, not shimmed.

### 1.1 Guru ingest — source of truth (locked for implementation)

This removes all ambiguity before coding.

| Topic | **Parity (mandatory)** | **Post-parity (not in §3)** |
|--------|------------------------|------------------------------|
| **Authoritative guru mirror** | **Polymarket Data API** incremental **wallet activity / trades** for the configured guru `proxyWallet`. Polling loop + persisted cursor; all guru signals that count for parity originate from **parsed API JSON** normalized in `venue/polymarket/data_api_client.py` + `ingestion/guru_stream.py`. | **RTDS (or any separate real-time WebSocket “activity” feed)** used only as a latency accelerator **after** it has a **frozen** contract in-repo: URL, subscribe payload, per-message schema, dedup key, and golden fixtures under `tests/fixtures/rtds/`. Until then, **do not implement RTDS** on the parity path. |
| **Watermark** | Monotonic **cursor** derived from **Data API** responses (e.g. timestamp + tie-breaker id — **exact fields chosen once** in code and documented in `guru_stream.py` docstring). Persisted in `strategy_store` (or equivalent). | N/A |
| **Dedup** | **Stable id** per activity row from API (e.g. transaction hash, or API-native trade/activity id if present). Same logical trade **never** emits two `GuruTradeSignal` facts. | If RTDS is added later: merged stream must **dedupe against the same key** as Data API or a provably equivalent mapping. |
| **Gap-fill** | On restart or lag: call Data API for `(last_committed_watermark, now]` and process rows **in order** through the same handler as steady-state poll. | RTDS may **not** replace gap-fill; Data API remains the recovery source. |

**Config for parity:** strategy YAML exposes **`guru.data_api_poll_interval`** (and related Data API parameters only). **Do not** expose `rtds_primary` / `rtds_shadow` / `poll_only` as parity modes—the parity path is **one**: Data API polling with the above semantics. (Scenario files may tune interval and caps only.)

---

## 2. Current Tyrex business scope to reproduce (concrete)

This restates what operators and the old **CONFIG_MODEL / OPERATIONS / LIVE_ARCHITECTURE** workflow implied for **guru copy**. Implementation must match these behaviors; exact YAML keys may differ but **capabilities** must not regress.

### 2.1 Guru ingestion

| Behavior | Requirement |
|----------|-------------|
| **Authoritative source (parity)** | **Data API only** — see §1.1. Incremental poll of guru wallet activity/trades; watermark + dedup + gap-fill **all** defined against API payloads. |
| **Historical “modes” (Tyrex v1)** | RTDS primary / shadow / poll-only were **Nautilus-era operational modes**. Native parity **does not** recreate those switches; it recreates **the same business outcome** (reliable, deduped guru activity → signals) via **one** Data API path. RTDS acceleration is **post-parity** (§1.1 right column, §13). |
| **Watermark** | As §1.1: persisted monotonic cursor from Data API. |
| **Dedup** | As §1.1: one signal per logical trade/activity id. |
| **Gap fill** | As §1.1: Data API backfill after `(watermark, now]`. |

### 2.2 Token filter

| Behavior | Requirement |
|----------|-------------|
| **Allowlist** | Only tokens in configured allowlist are eligible for copy; others skipped with **fact + reason**. |
| **Deny / resolution** | If configured, exclude resolved/dead/untradeable markets (Gamma/metadata); skip with **fact + reason**. |

### 2.3 Sizing and Layer-A-style filters

| Behavior | Requirement |
|----------|-------------|
| **`copy_scale`** | Multiplier from guru size/notional to **target** bot size before risk clamps. |
| **Conviction sizing** | Configurable curve: inputs from signal (e.g. conviction score); clamps min/max contribution; **facts** record factor applied. |
| **Static minimum** | Ignore guru trades below min notional/size (noise / fee control). |
| **Significance / conviction gate** | Trades below significance threshold skipped unless config disables. |
| **Exit interpretation** | Guru **sell** / reduce maps to **`ExitIntent` / `ReduceIntent`** per rules (full vs partial, dust threshold); facts record interpretation. |

### 2.4 Risk (fail-closed philosophy preserved)

| Behavior | Requirement |
|----------|-------------|
| **Per-order min/max notional** | Reject below min or above max with **ReasonCode**. |
| **Per-token deployment cap** | Venue-backed “deployment” for that token ≤ cap (accounting rule documented in `risk/deployment.py` + tests). |
| **Portfolio deployment cap** | Aggregate deployment across tokens ≤ cap. |
| **Capital gate (BUY)** | When enabled: sufficient **USDC balance** and **allowance** (or equivalent) from **WalletStore**; else deny with reason. |
| **Inventory gate (SELL)** | Sell qty ≤ `venue_position - reserved_in_flight`; ambiguous venue → **deny**. |
| **Kill switch** | Config flag: **all** new risk approvals denied immediately (optional policy: cancel bot orders — must be explicit in config). |
| **Concurrency / pacing** | Max concurrent bot orders, optional per-token lock or cooldown — config-driven; prevents guru bursts. |
| **Health / startup** | Until readiness criteria met, **no live aggressive** approvals (shadow may still run). Stale snapshot → fail-closed. |
| **Reason codes** | Every deny (and material approve) logged in **facts** with stable codes from `core/reason_codes.py`. |

### 2.5 Truth model (replaces Tier A/B wording)

| Behavior | Requirement |
|----------|-------------|
| **Venue truth** | Positions, open orders, balances/allowances from User WS + REST reconciliation. |
| **Local truth** | OMS in-flight, client order ids, retries; strategy watermark/dedup state. |
| **Reconciliation** | Periodic or event-driven compare; drift → **health fact** + risk fail-closed until cleared (per severity config). |
| **Manual / external activity** | Visible via venue streams; never “invented” locally. |

### 2.6 Reporting and operator workflow

| Behavior | Requirement |
|----------|-------------|
| **Facts** | JSONL (minimum): guru_signal, intent_created, risk_decision, oms_submit / oms_result, fill, reconcile, health. |
| **Manifest** | Run id, git sha, config hashes, start/end, schema version. |
| **Summarize** | CLI or script: counts, top deny reasons, join guru → intent → risk → oms (parity with old “summarize” **purpose**, not necessarily same CLI flags). |

### 2.7 Config usability (operator)

| Behavior | Requirement |
|----------|-------------|
| **Split** | **Strategy**, **risk**, and **runtime** settings in **separate** YAML files; **scenario** file **overlays** them without duplicating whole configs. |
| **Secrets** | API keys, private key material, **only** in environment / `.env` — never committed YAML. |

---

## 3. Copy-strategy parity = …

**Copy-strategy parity** is achieved when **all** of the following are true at once:

1. **Ingestion:** **Data API** guru activity path works as in §1.1–§2.1: durable **watermark**, **dedup**, and **gap-fill**; no duplicate guru signals across restart; exact cursor/dedup fields **documented in code**.
2. **Guru-follow logic:** token filter, `copy_scale`, conviction sizing, static/significance gates, and exit interpretation work per §2.3 with **facts** for every skip.
3. **Risk:** every gate in §2.4 is implemented; every deny carries **ReasonCode**(s) in facts; deployment math is **documented and tested**.
4. **OMS:** **one** queue per wallet; **shadow** path records would-submit facts without REST; **live** path places/cancels on Polymarket CLOB; **live** path runs **`venue/polymarket/heartbeat.py`** (or equivalent) on the **schedule Polymarket requires** to keep session/open orders valid; **heartbeat failure** sets **degraded health**, emits **`HealthChanged` + `risk_decision` deny** for new live aggressive orders until recovered (see §7); order identity correlates **intent_id → client_order_id → venue_order_id** in facts.
5. **Truth:** WalletStore and OrderStore reflect venue + local rules in §2.5; reconciliation emits **reconcile** + **health** facts.
6. **Startup:** Live aggressive orders are **blocked** until readiness; test proves deny-before / allow-after snapshot.
7. **Config:** Operator can run **named scenarios** composed from strategy + risk + runtime + overlay (§5); same code path for shadow vs live.
8. **Reporting:** A single run’s facts + manifest let an operator answer: *why was this guru trade skipped / approved / denied / filled?* using summarize or equivalent.

**Parity is a binary milestone:** either the **mandatory checklist (§12)** is all checked with **mandatory tests (§11)** green, or it is not.

---

## 4. Exact module map (parity delivery)

Only these packages/files are **required** to declare parity. Other directories in [ARCHITECTURE.md](ARCHITECTURE.md) may exist as **empty stubs** but must not distract delivery; **post-parity** modules are listed in §13.

```text
src/tyrex_pm/
  core/
    events.py, models.py, enums.py, ids.py, time.py, errors.py
    reason_codes.py
    bus.py

  venue/polymarket/
    auth.py, gamma_client.py, data_api_client.py
    market_ws.py, user_ws.py
    normalizers.py, rate_limits.py
    clob_execution.py, heartbeat.py          # live path

  state/
    market_store.py, wallet_store.py, order_store.py, strategy_store.py
    reconcile.py

  ingestion/
    guru_stream.py, historical_backfill.py
    market_stream.py, user_stream.py        # supervisors feeding bus

  signals/
    base.py, guru_copy_signal.py

  strategies/
    base.py
    guru_follow/strategy.py, sizing.py, filters.py

  risk/
    engine.py
    pretrade.py, deployment.py, capital.py, inventory.py
    kill_switch.py, health.py, concurrency.py

  execution/
    oms.py, router.py, order_builder.py, cancel_manager.py
    adapters.py                             # ShadowOMS, LiveOMS
    slippage.py, liquidity_guard.py         # minimal for parity

  reporting/
    schema_v2.py, facts.py, sinks/jsonl.py, summarize.py

  runtime/
    app.py, config.py, dependency_graph.py
    supervisors.py, healthchecks.py, modes.py
```

**Not required for parity (architecture reserved, implement later):** `protection/*`, `portfolio/*`, `features/*`, extra `signals/*` (event/indicator/ml), `universe_builder.py` beyond subscribing tokens guru needs, paper simulation engine, **RTDS / alternate real-time guru feed** (§1.1).

### 4.1 Identifiers and subscription mapping (locked)

| Concern | Owner | Rule |
|---------|--------|------|
| **Strategy-facing outcome id** | `core/ids.py` (`TokenId` or plain str per code choice) | Intents, `GuruTradeSignal`, allowlists, and risk deployment keys use **`token_id`**: the Polymarket **outcome / CLOB token id** (the id orders and positions use for that leg). |
| **Condition ID / market id** | `venue/polymarket/gamma_client.py` | Resolve **metadata** (resolution, titles, outcome index) and **condition_id → token_id** when the Data API returns a condition without a ready-made token id. **Strategies do not** take `condition_id` as the primary order key. |
| **Market WebSocket subscriptions** | `ingestion/market_stream.py` + `venue/polymarket/market_ws.py` | Subscribe using **Polymarket market-channel asset ids** (token ids) required for pricing; translate “tokens we care about” (guru allowlist + open positions) into the **subscription list**. |
| **User WebSocket** | `ingestion/user_stream.py` + `venue/polymarket/user_ws.py` | Authenticated **user** channel; `normalizers.py` maps raw order/fill payloads to **`token_id`** on every `UserOrderUpdated` / `UserTradeUpdated`. |
| **Guru Data API rows** | `ingestion/guru_stream.py` + normalizer in `data_api_client` / `normalizers.py` | Each row normalized to **`token_id`** (+ side, size, price, timestamps, dedup key) before `GuruTradeSignal` hits the bus. |

---

## 5. Exact config design (operator parity)

### 5.1 Files on disk

```text
config/
  risk/
    default.yaml                 # shared risk defaults
  runtime/
    default.yaml                 # logging, intervals, reporting dir
  strategies/
    guru_follow.yaml             # guru-follow-only knobs
  scenarios/
    shadow_guru.yaml             # overlays: execution_mode, caps, ingest
    live_guru.yaml
```

**Load order:** `risk/default.yaml` + `runtime/default.yaml` merged with **`--strategy`** `strategies/guru_follow.yaml` + **`--scenario`** `scenarios/<name>.yaml`. Later wins on scalar keys; lists/maps merge policy must be **defined once** in `runtime/config.py` (document the rule in code docstring).

### 5.2 Strategy YAML (`strategies/guru_follow.yaml`) — required sections

| Section | Contents |
|---------|----------|
| `guru` | `wallet` (proxy address), **`data_api_poll_interval_s`**, optional Data API page size / max backfill window (parity path only; see §1.1) |
| `filters` | `token_allowlist` (list), optional `min_notional_usd`, significance parameters, conviction enable + thresholds |
| `sizing` | `copy_scale`, conviction min/max multipliers / curve parameters |
| `exits` | dust threshold, full vs partial exit rules for guru sells |

### 5.3 Risk YAML (`config/risk/default.yaml`) — required sections

| Section | Contents |
|---------|----------|
| `notional` | per-order `min_usd`, `max_usd` (or size fields if preferred — one scheme only) |
| `deployment` | per-token cap, portfolio cap, **definition** string of what counts (filled, open orders, both — pick one and test) |
| `capital` | `enabled`, freshness max age |
| `inventory` | `sell_requires_venue_position` (always true for parity) |
| `kill_switch` | `enabled` |
| `concurrency` | max concurrent orders, optional cooldown ms |
| `readiness` | required flags before live trade: wallet snapshot freshness, user WS connected (if required), **CLOB heartbeat OK** (live only), market data if risk needs BBO |

### 5.4 Runtime YAML (`config/runtime/default.yaml`) — required sections

| Section | Contents |
|---------|----------|
| `execution_mode` | `shadow` \| `live` |
| `reporting` | `enabled`, `runs_dir` (e.g. `var/reporting/runs`) |
| `supervisors` | reconnect backoff, reconcile interval |
| `logging` | level, format |

### 5.5 Scenario overlay (`config/scenarios/*.yaml`)

Thin files: only overrides, e.g. `execution_mode: shadow`, stricter `deployment` caps for testing, faster `data_api_poll_interval_s` for integration tests.

### 5.6 Environment / `.env` only (never YAML)

| Variable | Purpose |
|----------|---------|
| Polymarket / CLOB API credentials | per Polymarket client docs |
| Private key / signer | order signing |
| Optional HTTP proxy | if used |

---

## 6. Exact runtime truth model

| Layer | Source of truth | Consumed by |
|-------|-----------------|-------------|
| **Venue positions / balances / allowances** | REST snapshots + User WS updates | `WalletStore` → `RiskContext` |
| **Venue open orders** | User WS + REST | `WalletStore` / `OrderStore` (designate single place for “resting at venue”) |
| **Local in-flight** | OMS queue state | `OrderStore` + OMS |
| **Guru watermark / dedup** | Durable store in `StrategyStore` (or SQLite single table) | `guru_stream` |
| **Stale** | Now - `last_wallet_sync_ts` > threshold | `risk/health.py` → deny live aggressive |

**Reconciliation loop:** compare venue snapshot to local order map; mismatches emit `ReconcileComplete` fact with `drift_flags`; **critical** drift denies new live risk.

**External trades:** always merge from venue; strategy does not simulate counterparty fills.

**ID consistency:** Risk and OMS never guess `token_id`; they use values from `WalletStore` / intents that were normalized from venue or guru pipeline per §4.1.

---

## 7. Exact OMS design (implementation-ready)

| Element | Rule |
|---------|------|
| **Entry** | Only `RiskEngine` output `ApprovedIntent` enqueues work (or explicit cancel from strategy/protection post-parity). |
| **Queue** | Single asyncio producer-consumer per wallet; **FIFO** unless cancel prioritization documented. |
| **Order identity** | `client_order_id` = unique string (uuid4 or prefixed run+seq); immutable mapping to `venue_order_id` after ack. |
| **Submit** | `order_builder` builds signed order → `clob_execution.place`; response normalized to bus events + facts. |
| **Cancel** | `cancel_manager` issues cancel by venue id; facts on result. |
| **Retries** | Transient errors: bounded retry with backoff; **same** `client_order_id` for idempotent retry policy document in code. |
| **Heartbeat (live parity)** | **`venue/polymarket/heartbeat.py`** runs as a **supervised task** for the **same wallet/session** as `LiveOMS`. It performs Polymarket’s required **session / keep-alive** calls on a **configurable interval** (defaults aligned to official CLOB docs). **On failure:** emit **`HealthChanged`**, set **`risk/health`** degraded, **deny** new **live** aggressive approvals until heartbeat succeeds again; emit **`health`** facts. **Shadow:** no CLOB heartbeat. |
| **Shadow** | `ShadowOMS`: full queue, validate builder, write `oms_submit` / simulated `oms_ack` facts, **no** HTTP. |
| **Live** | `LiveOMS`: real HTTP + **heartbeat task**; same fact schema as shadow. |

---

## 8. Exact risk design (philosophy + mapping)

**Philosophy:** **Fail-closed.** Missing inputs for a gate → **deny**. Ambiguous venue position → **deny** SELL. Degraded health → deny **live** aggressive unless scenario explicitly allows reduced mode (default: **no**).

**Pipeline order (fixed):** `kill_switch` → `health/readiness` (includes **live CLOB heartbeat OK** when `execution_mode=live`) → `concurrency` → `pretrade` (min/max) → `deployment` (token + portfolio) → `capital` (BUY) → `inventory` (SELL) → approve with optional **size clip**.

**Reason codes:** one primary code per deny; optional secondary detail string in fact JSON.

**Explainability:** `risk_decision` fact includes `reason_codes[]`, snapshot ids / timestamps used, and **not** free-form logs as substitute.

---

## 9. Exact guru-follow reproduction path (event chain)

1. **`guru_stream`** polls **Data API** (§1.1) → normalized **`GuruTradeSignal`** on bus per [EVENT_CATALOG.md](EVENT_CATALOG.md) (`source=rest` or `internal` after normalize).
2. **`guru_copy_signal`** enriches → **signal record** (facts: `guru_signal`).
3. **`guru_follow/strategy.py`** applies **`filters`** → skip with fact or proceed.
4. **`sizing`** applies **`copy_scale`** + conviction → **`IntentCreated`** (`EnterIntent` / `ExitIntent` / `ReduceIntent`).
5. **`RiskEngine.evaluate`** → **`RiskApproved`** or **`RiskRejected`** fact.
6. **`OMS`** (shadow or live) → **`OrderSubmitted`** / result facts; venue responses update stores via bus.
7. **`summarize`** joins run facts for operator.

No strategy module imports `venue` or `httpx`.

---

## 10. Ordered implementation phases (straight line to parity)

Each phase **must** complete its tests before the next starts.

### Phase 1 — Repository + core types + bus

| Item | Content |
|------|---------|
| **Goal** | Runnable package; publish/subscribe works. |
| **Files** | `pyproject.toml`, `src/tyrex_pm/__init__.py`, `core/{events,models,enums,ids,time,errors,reason_codes,bus}.py` |
| **Depends on** | — |
| **Tests** | Unit: handler receives event; reason codes importable. |
| **Operator-visible** | None yet. |

### Phase 2 — Polymarket read adapters

| Item | Content |
|------|---------|
| **Goal** | Raw WS/HTTP → normalized **DTOs** / internal events. |
| **Files** | `venue/polymarket/{auth,gamma_client,data_api_client,market_ws,user_ws,normalizers,rate_limits}.py` |
| **Depends on** | Phase 1 |
| **Tests** | Fixture JSON → expected `MarketBookUpdated` / `UserOrderUpdated` / Data API page parse. |
| **Operator-visible** | None (library only). |

### Phase 3 — State stores + reconciliation

| Item | Content |
|------|---------|
| **Goal** | Deterministic projections from events. |
| **Files** | `state/{market_store,wallet_store,order_store,strategy_store,reconcile}.py` |
| **Depends on** | Phase 1–2 |
| **Tests** | Event sequence → golden store snapshots; forced drift → reconcile flag. |
| **Operator-visible** | None. |

### Phase 4 — Reporting (mandatory path)

| Item | Content |
|------|---------|
| **Goal** | Every later phase emits **facts** to disk. |
| **Files** | `reporting/{schema_v2.py,facts.py,sinks/jsonl.py,summarize.py}` |
| **Depends on** | Phase 1 |
| **Tests** | Write `facts.jsonl` + `manifest.json`; summarize loads run. |
| **Operator-visible** | Empty run produces valid manifest. |

### Phase 5 — Config loader + scenario merge

| Item | Content |
|------|---------|
| **Goal** | Typed config matches §5; CLI entry accepts `--strategy` / `--scenario`. |
| **Files** | `runtime/config.py`, `config/**` tree as §5 |
| **Depends on** | Phase 1 |
| **Tests** | Merge order; unknown keys rejected; shadow vs live flag switches `OMS` binding only. |
| **Operator-visible** | Can point bot at scenario YAML. |

### Phase 6 — Risk engine (full mandatory gates)

| Item | Content |
|------|---------|
| **Goal** | All §8 gates; facts on every decision. |
| **Files** | `risk/{engine,pretrade,deployment,capital,inventory,kill_switch,health,concurrency}.py` |
| **Depends on** | Phase 3–4–5 |
| **Tests** | Table-driven: each **ReasonCode**; deployment cap; naked SELL denied; kill switch. |
| **Operator-visible** | Risk decisions visible in facts for synthetic intents. |

### Phase 7 — Shadow OMS + execution router

| Item | Content |
|------|---------|
| **Goal** | Single-writer queue; **no network**; full facts. |
| **Files** | `execution/{oms,router,order_builder,cancel_manager,adapters,slippage,liquidity_guard}.py` |
| **Depends on** | Phase 1–3–4–6 |
| **Tests** | Serial processing; duplicate client id rejected; `risk_decision` → `oms_submit` correlation. |
| **Operator-visible** | **Shadow** runs show full intent→risk→oms trail. |

### Phase 8 — Guru ingestion

| Item | Content |
|------|---------|
| **Goal** | **Data API–only** guru pipeline: §1.1 watermark, dedup, gap-fill; normalized `token_id`. |
| **Files** | `ingestion/{guru_stream.py,historical_backfill.py}`; persist in `strategy_store`; extend `data_api_client` / normalizers for activity rows |
| **Depends on** | Phase 2–4–5 |
| **Tests** | Recorded **Data API JSON fixtures**: duplicate suppressed; restart reloads watermark; gap batch processed in order; every emitted signal has dedup key in fact. |
| **Operator-visible** | Guru facts appear in runs from **live polling** or fixtures. |

### Phase 9 — Guru-follow strategy

| Item | Content |
|------|---------|
| **Goal** | §2.3 path from signal to intents. |
| **Files** | `signals/{base,guru_copy_signal}.py`, `strategies/{base,guru_follow/strategy,sizing,filters}.py` |
| **Depends on** | Phase 1–6–8 |
| **Tests** | Golden: guru events + config → expected intents + skip facts. |
| **Operator-visible** | End-to-end **shadow** intent generation from live or recorded guru feed. |

### Phase 10 — Runtime wiring + supervisors

| Item | Content |
|------|---------|
| **Goal** | Long-running `app`: market WS, user WS, guru, reconcile, strategy dispatch, OMS consumer. |
| **Files** | `runtime/{app,dependency_graph,supervisors,healthchecks,modes}.py`, `ingestion/{market_stream,user_stream}.py` |
| **Depends on** | Phase 2–7–9 |
| **Tests** | Integration: mock feeds, 60s soak, supervisor failure → health fact. |
| **Operator-visible** | Single command starts bot **shadow** or **live**. |

### Phase 11 — Live OMS + CLOB + heartbeat + startup gate

| Item | Content |
|------|---------|
| **Goal** | Real orders; **heartbeat** keeps session alive; heartbeat failure → health deny; readiness gates live until §2.4 satisfied. |
| **Files** | `venue/polymarket/{clob_execution,heartbeat}.py`; complete `LiveOMS`; wire `risk/health` + `healthchecks` for heartbeat status |
| **Depends on** | Phase 10 |
| **Tests** | Integration with **test wallet**: place+cancel; **forced heartbeat failure** → `HealthChanged` + new live intents denied until recovery; deny before snapshot / allow after. |
| **Operator-visible** | **Live** tiny-size trading with caps; ops can see heartbeat health in facts. |

**Additions (guru-follow ops):** optional **static BUY sizing** (`sizing.static_*`); **`notional.max_policy`** (`cap` \| `deny`) with explicit **`risk_decision`** fields; **capital gate** default-on in shared risk YAML (**`live_attest`** may disable); **`oms_reject`** fact on venue submit failure without killing the run loop.

### Phase 12 — Parity gate

| Item | Content |
|------|---------|
| **Goal** | Declare **copy-strategy parity** (§3 + §12 + §11). |
| **Files** | `tests/parity/**` goldens; update [Docs/OPERATIONS.md](../../OPERATIONS.md) with native runbook (paths, flags, greps). |
| **Depends on** | Phase 11 |
| **Tests** | Full **mandatory test plan (§11)**; manual sign-off on checklist. |
| **Operator-visible** | Production-style shadow/live runs with documented ops. |

---

## 11. Mandatory test plan (before parity is declared)

| # | Test | Purpose |
|---|------|---------|
| T1 | Reason-code table tests | Every deny path emits expected code. |
| T2 | Deployment + inventory | Caps and naked SELL enforced on synthetic `WalletStore` / `OrderStore`. |
| T3 | Guru golden pipeline | **Data API JSON** fixtures → normalized signals → expected intents + facts. |
| T4 | Watermark durability | Restart mid-stream does not duplicate signals (**Data API** cursor persisted). **Implemented:** `tests/test_t4_guru_watermark_restart.py`. |
| T5 | Shadow E2E | Mock venue; full bus path facts join guru→risk→oms. |
| T6 | Live smoke | **`tyrex-pm live-attest`** (scenario `live_attest`): one intentional **post + cancel** via **`SingleWriterOMS` + `LiveOMS`**, same supervised **heartbeat / venue refresh / user WS** stack as `tyrex-pm run` live. Facts: `live_attest`, `intent_created`, `risk_decision`, `oms_submit`, `oms_cancel`, `reconcile`, `run_summary.json`. **Tests:** `tests/test_live_attest_unit.py` (mocked); real wallet requires `pip install tyrex-pm[live]` + `TYREX_PRIVATE_KEY`. Optional: `TYREX_LIVE_SMOKE=1` heartbeat-only pytest. |
| T7 | Reconcile drift | Injected mismatch → health + deny live. |
| T8 | Summarize | Join keys resolve end-to-end on a real run directory. **Implemented:** `summarize_run` includes `join_audit`; `tests/test_t8_summarize_joins.py`. |

All **T1–T8** must pass. No parity without them.

**Automated suite mapping (closeout):**

| # | Primary tests |
|---|----------------|
| T1 | `tests/test_risk_engine.py` — kill switch, concurrency, stale wallet, reconcile drift, naked sell → reason codes |
| T2 | `tests/test_deployment.py` — deployment caps + projections; `test_risk_engine` SELL / inventory |
| T3 | `tests/test_guru_strategy_golden.py`, `tests/test_data_api_normalize.py`, `tests/test_guru_stream.py` |
| T4 | `tests/test_t4_guru_watermark_restart.py` |
| T5 | `tests/test_shadow_e2e.py` |
| T6 | `tests/test_live_attest_unit.py`, `tests/test_t6_live_oms_unit.py` (mocked paths); **real wallet:** operator `tyrex-pm live-attest` + archive `ops/parity_attestation/` |
| T7 | `tests/test_pipeline_reconcile_deny.py`, `tests/test_reconcile_store.py` |
| T8 | `tests/test_t8_summarize_joins.py` |

**CI / default pytest:** all of the above **pass**; **`test_t6_real_clob_heartbeat_smoke`** is **skipped** unless `TYREX_LIVE_SMOKE=1` (optional heartbeat-only real CLOB ping per T6 note). That skip is **acceptable**: mandatory real-wallet proof is the **`live-attest`** binary attestation, not this optional pytest.

---

## 12. Mandatory parity checklist (strict)

**Parity (Phase 12):** §11 is green with the documented optional skip, and live attestation is signed off in **`ops/parity_attestation/ATTESTATION_RECORD.md`** (§12 row below).

- [x] **Data API** guru ingest: poll loop, watermark, dedup, gap-fill verified (T3, T4 + fixtures)
- [x] **token_id** normalization for guru rows and intents
- [x] **User channel (live):** authenticated `wss://ws-subscriptions-clob.polymarket.com/ws/user` ingestion updates `WalletStore` open orders + confirmed fills; merges with REST snapshot; **staleness** (`require_user_ws_live` + `TYREX_USER_WS_*`) fail-closes **new live aggressive** orders. **`TYREX_USER_WS_DISABLE=1`** + `readiness.require_user_ws_live: false` documents **REST-only** venue-truth mode when WS cannot run.
- [x] Token allowlist + skip facts
- [x] **Untradeable / resolved markets (when configured):** `filters.exclude_untradeable_markets` + Gamma `/markets?clob_token_ids=…` → skip with `market_untradeable` / `market_metadata_unavailable` (default **off**)
- [x] `copy_scale` + conviction + static/significance filters + exit interpretation + facts
- [x] Min/max notional; token + portfolio deployment; capital gate; inventory gate; kill switch; concurrency
- [x] Fail-closed on stale/missing snapshot, user-stream staleness (when required), or critical drift
- [x] Shadow: full intent→risk→oms facts without CLOB
- [x] **Live parity attestation (binary):** Operator ran **`tyrex-pm live-attest`** on a **designated wallet** with scenario **`live_attest`**; venue confirmed **`POST /order`** and **`DELETE /order`** **200** with **`POST /v1/heartbeats`** **200** (proxy signing). Record + archive: **`ops/parity_attestation/ATTESTATION_RECORD.md`** (optional copy of `var/reporting/runs/<run_id>/` under **`ops/parity_attestation/runs/`**). CI remains mock-only for T6 pytest.
- [x] Startup readiness gates live aggressive (wallet sync + heartbeat + CLOB session + optional user WS freshness on `HealthRuntime` / `RiskContext`)
- [x] Scenario + strategy + risk + runtime composition (§5)
- [x] Reporting + summarize (T8) with `join_audit`
- [x] OPERATIONS.md documents native run entrypoints

---

## 13. Explicit non-parity items (do not block declaration)

| Item | Status |
|------|--------|
| **Protection / virtual TP/SL / lot recovery** | **Post-parity** module; not required for §3. |
| **Paper / simulated fills** | **Not required**; shadow facts are enough. |
| **`feature_store` / rich microstructure features** | **Post-parity**. |
| **ML / indicator / multi-signal strategies** | **Post-parity**; architecture reserves `signals/` slots. |
| **Advanced execution (TWAP, smart peg, cross-market)** | **Post-parity**. |
| **`portfolio` analytics beyond facts** | **Post-parity**. |
| **Universe builder / market scoring** | **Post-parity**; guru path may subscribe only tokens it needs. |
| **RTDS / WebSocket guru activity** | **Post-parity** until URL, subscribe contract, payload schema, dedup mapping to Data API ids, and `tests/fixtures/rtds/**` exist (§1.1). |

---

## 14. Final milestone statement

**Copy-strategy parity is achieved at the end of Phase 12** when:

- §3 **Copy-strategy parity** definition is satisfied, and  
- §12 **Mandatory parity checklist** is fully checked, and  
- §11 **Mandatory test plan** **T1–T8** is green.

No additional architecture approval is required to start Phase 1.

---

## 15. Nautilus-era baggage to omit

Do not implement: `TradingNode`, Nautilus `Actor`/`Strategy`, framework `Cache`/`Portfolio`/`ExecEngine`, or reconciliation to Nautilus internal IDs. **Venue + local stores** replace Tier A/B framing.

---

## 16. True blockers (external)

| Blocker | Mitigation |
|---------|------------|
| Polymarket **API credentials** / signing for T6 | Dedicated test wallet; secrets in CI vault or skip T6 in CI with manual attestation |
| **Deployment accounting** ambiguity | Lock one rule in `risk/deployment.py` + tests in Phase 6 — not a design fork |
| **Data API contract drift** | Polymarket may change field names; mitigate with versioned fixtures + normalizer tests (T3). |

---

*Document version: 2.2 — Phase 12 parity closeout: §11 test mapping, live attestation archived under `ops/parity_attestation/`, §12 live gate complete.*

---

## Phase 12 — parity declared (native rebuild)

**Copy-strategy parity** per §3 and **mandatory checklist** §12 are **complete** as of document **2.2**: automated **T1–T8** coverage passes with the **documented optional skip** for `TYREX_LIVE_SMOKE`; real-wallet **live-attest** evidence is recorded under **`ops/parity_attestation/`**.
