# Internal event catalog (native PM)

Typed events on the **async bus** between adapters, stores, strategy, risk, OMS, protection, and reporting.

**Companion:** [ARCHITECTURE.md](ARCHITECTURE.md) · [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)

**Parity:** All events below are **contracts** for the bus implementation. **`Protection*`** events are required **only after** the protection module ships; **copy-strategy parity** does not require emitting them ([IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) §3, §13).

**`GuruTradeSignal`:** for parity, produced only from **Data API**–normalized rows (`envelope.source` typically `rest`). **RTDS-sourced** guru events are **post-parity** ([IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) §1.1).

*Earlier v1→native concept mapping: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) §9, §15.*

---

## 1. Envelope

**Versioning:** each payload may carry `schema_version` (implementation detail in `core.events`).

| Field | Type | Notes |
|-------|------|--------|
| `event_id` | UUID | Unique per emission. |
| `schema_version` | int | Bump on breaking changes. |
| `ts_venue` | datetime? | If known from payload. |
| `ts_recv` | datetime | Local receipt time. |
| `source` | enum | `market_ws`, `user_ws`, `rest`, `internal`, `replay` |
| `payload` | union | One of the events below |

---

## 2. Market / data events

| Event | Key fields | Producers | Consumers |
|-------|------------|-----------|-----------|
| `MarketBookUpdated` | `token_id`, `bids[]`, `asks[]`, `seq?` | market_ws, replay | MarketStore, features, protection, risk (indirect) |
| `MarketStatusChanged` | `token_id`, `status`, `detail?` | market_ws, gamma | MarketStore, strategies |
| `TradePrinted` | `token_id`, `price`, `size`, `side`, `ts?` | market_ws | FeatureStore, strategies |
| `UniverseRotated` | `added_token_ids[]`, `removed_token_ids[]` | universe_builder | market_stream, strategies |

---

## 3. Wallet / user events

| Event | Key fields | Producers | Consumers |
|-------|------------|-----------|-----------|
| `UserOrderUpdated` | `venue_order_id`, `client_order_id?`, `state`, `token_id`, `side`, `price`, `remaining`, `raw?` | user_ws, rest reconcile | OrderStore, WalletStore, OMS |
| `UserTradeUpdated` | `trade_id?`, `order_id`, `token_id`, `price`, `size`, `fee?`, `ts?` | user_ws | OrderStore, WalletStore, protection |
| `WalletSnapshotUpdated` | `positions[]`, `open_orders[]`, `balances?`, `allowances?`, `sync_id` | reconcile, rest | WalletStore, risk |
| `CollateralSnapshotUpdated` | `wallet`, `usdc_balance`, `allowance?`, `sync_id` | rest | WalletStore, risk |

---

## 4. Guru / research events

| Event | Key fields | Producers | Consumers |
|-------|------------|-----------|-----------|
| `GuruTradeSignal` | `guru_wallet`, `token_id`, `side`, `size`, `price?`, `tx_ref?`, `ts` | guru_stream | signals, strategies |
| `SignalGenerated` | `signal_type`, `payload`, `correlation_id` | signals | strategies, reporting |

---

## 5. Strategy / intent events

| Event | Key fields | Producers | Consumers |
|-------|------------|-----------|-----------|
| `IntentCreated` | `intent: Intent`, `strategy_id` | strategies | reporting, risk |
| `IntentAmended` | `intent_id`, `patch` | strategies (rare) | reporting, risk |

**`Intent` variants (models):** `EnterIntent`, `ExitIntent`, `ReduceIntent`, `CancelIntent`, `StateRequest` (read-only diagnostics).

---

## 6. Risk events

| Event | Key fields | Producers | Consumers |
|-------|------------|-----------|-----------|
| `RiskEvaluated` | `intent_id`, `decision: RiskDecision`, `reason_codes[]` | risk | reporting, strategies (async) |
| `RiskApproved` | `approved_intent: ApprovedIntent` | risk | OMS, reporting |
| `RiskRejected` | `intent`, `reason_codes[]`, `detail?` | risk | reporting |

---

## 7. Execution events

| Event | Key fields | Producers | Consumers |
|-------|------------|-----------|-----------|
| `OrderSubmitted` | `client_order_id`, `venue_order_id?`, `request` | OMS | OrderStore, reporting |
| `OrderSubmitFailed` | `client_order_id`, `error`, `retryable` | OMS | OrderStore, health, reporting |
| `OrderCanceled` | `venue_order_id`, `reason?` | OMS / user_ws | OrderStore, protection |
| `OrderFilled` | `fill: FillEvent` | user_ws / OMS | WalletStore, protection, portfolio |

---

## 8. Protection events

| Event | Key fields | Producers | Consumers |
|-------|------------|-----------|-----------|
| `ProtectionArmed` | `lot_id`, `policy` | protection | reporting |
| `ProtectionTriggered` | `lot_id`, `trigger`, `exit_intent` | protection | risk |
| `ProtectionCanceled` | `lot_id`, `reason` | protection | reporting |

---

## 9. Health / ops

| Event | Key fields | Producers | Consumers |
|-------|------------|-----------|-----------|
| `HealthChanged` | `component`, `status`, `detail?` | supervisors | risk, reporting |
| `ReconcileComplete` | `sync_id`, `drift_flags[]` | reconcile | risk, reporting |

---

## 10. Structured reporting facts (`facts.jsonl`, schema v2)

These are **JSONL rows** (not async bus envelopes) emitted by `tyrex-pm run` and `tyrex-pm live-attest`. See `tyrex_pm.reporting.schema_v2`.

| `fact_type` | Role |
|-------------|------|
| `health` | Run start/stop; live heartbeat transitions may appear as separate rows with payload `event`. |
| `guru_poll` | **Operator diagnostics:** Data API or fixture poll — `new_signals`, `raw_rows`, `pages_fetched`, `guru_wallet_configured`, etc. Explains hollow runs when `new_signals` is 0. |
| `guru_signal` | Normalized guru row entering the copy pipeline. |
| `strategy_skip` | Strategy declined to emit intents (reason code). |
| `intent_created` | Strategy or attestation intent. |
| `risk_decision` | Risk approve/deny. |
| `oms_submit` | OMS place ack; payload **`oms_result`** (canonical string); legacy **`shadow_result`** still readable via `summarize_run` / `get_oms_result_text`. |
| `oms_reject` | Live OMS place failed (e.g. venue **400**); payload includes **`status_code`**, **`error_msg`**, **`client_order_id`**. Does **not** stop the run loop. |
| `oms_cancel` | OMS cancel ack; **`oms_result`** canonical. |
| `reconcile` | `drift_flags` vs venue/local. |
| `live_attest` | Phases of **`tyrex-pm live-attest`** (`bootstrap`, `readiness`, `complete`, failures). |

---

## 11. Canonical interfaces (code contracts)

```python
class Strategy(Protocol):
    def on_event(self, event: Event, ctx: StrategyContext) -> list[Intent]: ...

class RiskEngine(Protocol):
    def evaluate(self, intent: Intent, ctx: RiskContext) -> RiskDecision: ...

class OMS(Protocol):
    async def submit(self, approved: ApprovedIntent) -> SubmitResult: ...
    async def cancel(self, cancel: CancelIntent) -> CancelResult: ...

class ProtectionEngine(Protocol):
    def on_fill(self, fill: FillEvent) -> None: ...
    def on_market(self, market_event: MarketEvent) -> list[ExitIntent]: ...
```

`StrategyContext`: read-only `MarketState`, `WalletState`, `StrategyState`, optional `FeatureState`.

`RiskContext`: venue-backed wallet snapshot id, health flags, deployment inputs, correlation ids.
