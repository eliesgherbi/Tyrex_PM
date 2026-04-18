# Tyrex_PM — final native Polymarket architecture (single target)

**Status:** authoritative **final** architecture for the rebuild. There is no alternate framework-centric track.

**Companion:** [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) · [COPY_STRATEGY_SCOPE.md](COPY_STRATEGY_SCOPE.md)

**External references:** [Polymarket WebSocket overview](https://docs.polymarket.com/market-data/websocket/overview), [List markets](https://docs.polymarket.com/api-reference/markets/list-markets), [Create order](https://docs.polymarket.com/trading/orders/create)

---

## 1. Executive decision (final)

Tyrex_PM is a **Polymarket-native**, **async**, **event-driven** trading system. The **center** of the runtime is:

1. **Venue adapters** (`venue/polymarket/`) that speak **Market WS**, **User WS**, **CLOB REST**, **Gamma**, and **Data API**—with normalization into **internal events**.
2. A **typed event bus** (`runtime/bus.py` or `core/bus.py`) that moves events between adapters, **state stores**, **ingestion supervisors**, **strategies**, **risk**, **OMS**, **protection**, and **reporting**.
3. A **single-writer OMS per wallet** (`execution/oms.py`) that is the **only** component allowed to submit/cancel on the CLOB for that wallet, including **heartbeat**, **retries**, and **order-id / correlation** discipline.
4. **Explicit state stores** (`state/`) for market, wallet/venue, orders, strategy, and optional features—**no** monolithic “portfolio object” that mixes concerns.
5. **Thin strategies** (`strategies/`) that only turn **normalized events + signal outputs** into **intents**; **no** raw venue I/O in strategies.
6. **First-class risk** (`risk/`) as a reusable `RiskEngine` with **stable reason codes**, **fail-closed** defaults, and **no** embedding of policy inside strategy or OMS (OMS may enforce mechanical limits; risk owns business gates).
7. **First-class reporting** (`reporting/`) emitting **versioned facts** from day one, correlated with intent/order/reconcile lifecycles.

**Performance:** maximize throughput within **Polymarket rate limits** via batching, subscription discipline, efficient normalization, and **single-writer** OMS serialization—not via generic exchange abstractions.

**Future strategies:** the same pipeline supports guru-follow **and** event, indicator, ML, and multi-signal strategies via **`signals/`**, **`features/`**, and composed **`strategies/`** packages.

---

## 2. Final module map (`src/tyrex_pm/`)

```text
src/tyrex_pm/
  core/
    events.py              # Discriminated union of internal events (+ envelope)
    models.py              # Intent, ApprovedIntent, Fill, OrderSnapshot, etc.
    enums.py               # Side, OrderStyle (GTC, FOK, FAK, …), HealthState, …
    ids.py                 # NewType wrappers for token_id, order ids, run_id
    time.py
    errors.py
    reason_codes.py        # Stable risk / health reason strings (or enum)
    bus.py                 # Async publish/subscribe dispatcher (optional: core vs runtime)

  venue/
    polymarket/
      auth.py
      gamma_client.py      # Markets, metadata, discovery
      data_api_client.py   # Activity, historical pages, backfill
      clob_execution.py    # Place / cancel / batch surfaces
      market_ws.py
      user_ws.py
      heartbeat.py         # Session / keep-open-orders alive integration
      normalizers.py       # Raw → core.events / DTO patches
      rate_limits.py

  state/
    market_store.py
    wallet_store.py        # Positions, balances, allowances; venue sync metadata
    order_store.py         # Local lifecycle + venue id mapping + in-flight
    strategy_store.py      # Dedup watermarks, strategy-private counters
    feature_store.py       # Optional derived features cache
    reconcile.py           # Drift detection; emits reconcile events / health

  ingestion/
    market_stream.py       # Supervisor: subscribe set, reconnect, fan-in
    user_stream.py
    guru_stream.py         # Data API poll (parity); dedup; watermark; gap-fill (RTDS post-parity)
    historical_backfill.py
    universe_builder.py    # Which token_ids / markets are live

  signals/
    base.py
    guru_copy_signal.py    # Normalized guru trade → Signal record(s)
    event_signal.py        # Stub for future
    indicator_signal.py
    ml_signal.py

  features/
    orderbook_features.py
    trade_features.py
    market_microstructure.py
    market_regime.py
    pnl_features.py

  strategies/
    base.py
    guru_follow/
      strategy.py
      sizing.py            # copy_scale, conviction mapping
      filters.py           # Layer-A-style composition
    # future: indicator_alpha/, ml_alpha/, …

  risk/
    engine.py              # Composed RiskEngine façade
    pretrade.py            # Notional min/max, basic sanity
    deployment.py          # Token + portfolio deployment caps (venue-backed inputs)
    exposure.py
    capital.py             # Balance + allowance gate
    inventory.py           # SELL inventory gate
    health.py              # Degraded modes feeding fail-closed
    kill_switch.py
    concurrency.py         # Max in-flight guru orders, cooldowns, per-token locks
    exit_policy.py         # Optional hooks for exit-specific risk rules

  execution/
    oms.py                 # THE single-writer per wallet
    router.py              # Chooses order style, price, aggression (no policy mixing)
    order_builder.py       # Builds signed / encoded orders per CLOB rules
    cancel_manager.py
    slippage.py
    liquidity_guard.py
    adapters.py            # NoOpOMS (shadow), LiveOMS (CLOB)

  protection/
    virtual_exits.py       # TP/SL, time exits (future)
    lot_tracker.py
    trigger_eval.py
    recovery.py

  portfolio/
    pnl.py
    positions.py
    attribution.py
    budgets.py             # Read-side analytics; not a write gateway

  reporting/
    facts.py               # Fact helpers + emitters
    schema_v2.py           # Versioned fact types (v1 legacy not required)
    sinks.py               # JSONL, optional SQLite
    summarize.py

  runtime/
    app.py                 # Main async entry
    config.py              # Load + validate typed config + scenarios
    dependency_graph.py    # Wire bus, stores, supervisors, engines
    supervisors.py         # Task groups, restart policy
    healthchecks.py
    modes.py               # live | shadow | paper (behavior flags)
```

**Package naming:** keep top-level **`tyrex_pm`** for continuity; internals follow the layout above.

**Parity vs post-parity:** The tree above is the **long-term** module layout. **Copy-strategy parity** implements only the subset listed in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) §4; `protection/`, `portfolio/`, extra `signals/*`, rich `features/*`, optional `universe_builder`, and **RTDS / WS guru acceleration** are **post-parity** (same architecture, later PRs). **Guru mirror for parity = Data API poll** only ([IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) §1.1).

**Guru pipeline note:** `guru_stream.py` is **Data API + watermark** for parity; RTDS wiring is **not** in the parity subtree.

---

## 2.1 Identifiers and subscription mapping (locked)

| Concern | Owner |
|---------|--------|
| **Canonical order/signal id** | **`token_id`** (outcome / CLOB token id) in `core/ids.py` and all intents, guru signals, allowlists, deployment keys. |
| **condition_id / market metadata** | **`venue/polymarket/gamma_client.py`**: resolve condition → tokens, resolution state; not the primary key strategies use to place orders. |
| **Market WS** | **`ingestion/market_stream.py`** + **`market_ws.py`**: subscriptions use **asset / token ids** per Polymarket market channel rules. |
| **User WS** | **`ingestion/user_stream.py`** + **`user_ws.py`**: authenticated user stream; **`normalizers.py`** ensures every order/fill carries **`token_id`**. |
| **Guru rows** | **`data_api_client.py`**, **`normalizers.py`**, **`guru_stream.py`**: every activity row becomes a **`GuruTradeSignal`** with **`token_id`** before it hits the bus. |

---

## 3. Core interfaces (summary)

| From | To | Interface |
|------|-----|-----------|
| Ingestion / venue | Bus | `publish(NormalizedEvent)` |
| Bus | Stores | `apply_event(store, event)` idempotent handlers |
| Signals | Strategies | `Signal` records + `SignalContext` readers |
| Strategies | Risk | `Intent` variants (`Enter`, `Exit`, `Reduce`, `Cancel`, `StateRequest`) |
| Risk | OMS | `ApprovedIntent` or deny with `ReasonCode[]` |
| Protection | Risk | `ExitIntent` / `CancelIntent` (never bypass risk) |
| OMS | Bus | `OrderSubmitted`, `OrderFilled`, … |
| All | Reporting | `emit_fact(Fact)` at decision points |

**Strategy:**

```python
class Strategy(Protocol):
    def on_event(self, event: Event, ctx: StrategyContext) -> list[Intent]: ...
```

**Risk:**

```python
class RiskEngine(Protocol):
    def evaluate(self, intent: Intent, ctx: RiskContext) -> RiskDecision: ...
```

**OMS:**

```python
class OMS(Protocol):
    async def submit(self, approved: ApprovedIntent) -> SubmitResult: ...
    async def cancel(self, cancel: CancelIntent) -> CancelResult: ...
```

**Protection:**

```python
class ProtectionEngine(Protocol):
    def on_fill(self, fill: FillEvent) -> None: ...
    def on_market(self, market_event: MarketEvent) -> list[ExitIntent]: ...
```

---

## 4. State model (who owns what)

| Store | Owns | Does **not** own |
|-------|------|-------------------|
| **MarketStore** | BBO, book, trades, market status | Wallet balances |
| **WalletStore** | Positions, balances, allowances, last sync ids | Order lifecycle internals |
| **OrderStore** | Client/venue ids, state machine, partial fills | Strategy sizing logic |
| **StrategyStore** | Dedup keys, watermark, guru processing offsets | Venue positions |
| **FeatureStore** | Derived features | Raw WS payloads |
| **Reconcile** | Compare venue vs local; flags | Not the OMS writer |

---

## 5. Venue truth vs local truth

- **Venue truth:** last good snapshot from **User WS + REST** for positions, open orders, balances; authoritative for **inventory** and **deployment** accounting.
- **Local truth:** OMS in-flight submits, retries, provisional client ids, protection lots, strategy watermarks.
- **Reconciliation:** on drift, emit facts + set health; risk moves **fail-closed** until cleared (configurable severity table).
- **Manual / external activity:** always absorbed via **venue streams first**; local state updated to match or flagged if inconsistent.

---

## 6. OMS design (single writer)

**Ownership:** exactly one `LiveOMS` instance per **trading wallet**; all CLOB submits/cancels go through it.

**Submit/cancel:** async queue, strict serialization per wallet; idempotent command keys for safe retries.

**Retries:** classify venue errors into retryable vs terminal; exponential backoff with cap; never double-submit without idempotency key discipline.

**Heartbeat:** **`heartbeat.py`** is **mandatory on live**: supervised keep-alive for the CLOB session; **failure → degraded health** and **risk denies new live aggressive orders** until recovery; facts record state. Optional **auto-cancel** on heartbeat loss is **config-only**, default off unless ops enable.

**Marketable vs resting:** `router.py` chooses **limit** price and **FOK/FAK/GTC** (names per actual CLOB API) using **MarketStore** top-of-book; strategies provide **intent targets**, not wire format.

**Identity / correlation:** `run_id`, `intent_id`, `client_order_id`, `venue_order_id` carried across facts; OMS is the mapping authority.

---

## 7. Shadow vs live (no Nautilus)

- **Shadow:** `ShadowOMS` + full risk + full facts; **no** CLOB HTTP. Fakes or simulates fills are **not** part of copy-strategy parity.
- **Live:** `LiveOMS` + CLOB + heartbeat; same fact schema as shadow.
- **Paper / simulated-fill engine:** **post-parity** only if product later demands it.

---

## 8. Alignment with repo docs

Top-level `Docs/Architecture.md` should be updated in a follow-up PR to point here as the **implementation source of truth**, or be edited to match this text—avoid three conflicting architectures.
