# Architecture

**Hub:** [README.md](README.md) · **Live truth:** [LIVE_ARCHITECTURE.md](LIVE_ARCHITECTURE.md) · **Modules:** [modules/README.md](modules/README.md)

---

## 1. What Tyrex_PM is

A modular Polymarket trading stack composed of:

- a small **runtime** (`runtime.app`, supervisors, coordinator),
- **venue adapters** for Polymarket (CLOB REST, user/market WS, Gamma, Data API),
- **explicit state stores** (`WalletStore`, `OrderStore`, `AllocationLedger`, `MarketStateStore`, `StrategyStore`),
- **thin strategies** that emit `Intent`s via a generic `Strategy.on_signal`,
- a **fail-closed `RiskEngine`** that produces `RiskDecision`s,
- an **`ExecutionPlanner`** that chooses concrete order style/price between risk pre-check and OMS,
- a **single-writer OMS** (shadow or live) for submit/cancel,
- a reusable **`protection/` overlay** (TP/SL) that emits urgent `ExitIntent`s,
- a **structured reporting** layer (`facts.jsonl` per run).

The official runtime spine is **generic and procedural** (`Signal → Strategy → Intent → RiskEngine → ExecutionPlanner → SingleWriterOMS → Venue`). Guru copy is **one** signal source/strategy, not the spine. NautilusTrader is not the runtime spine, and the `core/bus.py` event bus stays deferred ("procedural now, event-ready later"); the bot owns its own state machines and reconcile.

---

## 2. Core principles

| Principle | What it means in code |
|-----------|------------------------|
| **One writer per wallet** | `SingleWriterOMS` serializes `submit`/`cancel` onto one queue (`execution/oms.py`). |
| **Strategies never touch the venue** | `GuruFollowStrategy` consumes a normalized `GuruCopySignal` and returns `Intent`s. No HTTP/WS. |
| **Separated state** | `WalletStore` (venue truth), `OrderStore` (local OMS), `AllocationLedger` (strategy ownership), `StrategyStore` (guru dedup). |
| **Fast path vs slow path** | User WS is primary live truth; REST `/data/orders`, `/balance-allowance`, `/positions` are bootstrap + repair backstop. |
| **Venue truth vs local truth** | See [LIVE_ARCHITECTURE.md](LIVE_ARCHITECTURE.md) for reconcile state machines (provisional repair, venue adoption, WS-terminal tombstones). |
| **Fail-closed risk** | `RiskEngine.evaluate_intent` returns a `RiskDecision` with a stable `reason_code`. Missing prices, stale wallet, drift, missing capital all deny. |
| **Reporting is first-class** | Every decision emits a fact to `facts.jsonl` keyed by `run_id` + `correlation_id`. |
| **Quantized USD evidence** | `risk/evidence_format.py` standardizes 6-decimal USD strings in facts so reports are diff-friendly. |

---

## 3. Runtime diagram

```mermaid
flowchart TB
  subgraph Venue[Polymarket]
    CLOB[CLOB REST + WS]
    DataAPI[Data API]
    Gamma[Gamma]
  end

  subgraph Adapters[venue/polymarket]
    Bridge[PyClobBridge]
    UserWS[user_ws]
    DAClient[DataApiClient]
    GammaCl[GammaClient]
    Norm[normalizers]
  end

  subgraph Ingest[ingestion]
    GuruPoll[guru_stream.poll_guru_incremental]
    UserIngest[user_stream.run_user_ws_ingest]
  end

  subgraph State[state]
    Wallet[WalletStore]
    Orders[OrderStore]
    Ledger[AllocationLedger]
    Strat[StrategyStore]
    Reconcile[reconcile_open_orders]
  end

  subgraph Strategy[strategies / signals]
    Sig[GuruCopySignal]
    GF[GuruFollowStrategy]
  end

  subgraph Risk[risk]
    Engine[RiskEngine.evaluate_intent]
  end

  subgraph Exec[execution]
    OMS[SingleWriterOMS]
    Live[LiveOMS]
    Shadow[ShadowOMS]
  end

  subgraph Report[reporting]
    Facts[JsonlSink → facts.jsonl]
  end

  subgraph RT[runtime]
    Coord[RuntimeCoordinator]
    Supers[supervisors]
  end

  DataAPI --> DAClient --> GuruPoll --> Strat
  CLOB --> UserWS --> UserIngest --> Wallet
  CLOB --> Bridge
  Bridge --> Wallet
  Strat --> GF
  GuruPoll --> Sig --> GF --> Engine
  Engine --> OMS --> Live --> Bridge
  OMS --> Shadow
  Coord --> Wallet
  Coord --> Orders
  Coord --> Reconcile
  Supers --> Reconcile
  Coord --> Engine
  Engine --> Facts
  Reconcile --> Facts
  GuruPoll --> Facts
  OMS --> Facts
```

Component owners (one writer each): `runtime.app` wires everything; `RuntimeCoordinator` holds shared state; supervisors (heartbeat, venue refresh, provisional repair, user-WS staleness) are the only producers of their respective truth deltas.

**Non-guru CLI paths.** `simple_signal_test` (`fixture_signal_run.py`) proves Phase 1 + optional Phase 3 normal entry. `validation_harness` (`validation_harness_run.py`, Phase 4.5) exercises urgent exit, stale-book deny, protection registration/trigger, and live-read-only market data — paths `simple_signal_test` cannot reach. Live wiring adds `FinalityWaiter` (allocation-final wait after BUY) and `ProtectionSupervisor` (periodic protection tick loop). **Phase 4.6** adds `paired_binary` — production strategy with long-running monitor loop. Entry uses reconciled qty (user-WS finality primary; allocation clamp grace prevents REST lag zeroing). Status: **implemented · shadow-validated · live PB-Level 2/3 ready for re-run** ([phase_4_6 §18](Implementation/architecture_enhance/phase_4_6_paired_binary_strategy_production_protection.md#18-live-truth-sources-and-monitoring-reliability)).

**Market data + protection runtime (P4.5).** When `market_data.enabled`, `app.py` attaches `MarketStateStore` and starts REST bootstrap/refresh (`market_data_runtime.py`). When `protection.enabled`, `protection_runtime.py` initializes `ProtectionMonitor` and registers after allocation-final BUY fills via the pipeline hook.

---

## 4. Three engines

1. **Data engine** — `ingestion/*` + `venue/polymarket/*` turn Polymarket feeds into normalized `GuruTradeSignal`, `OpenOrderView`, `WalletPosition`, and `TradeFillRecord` records that flow into `state/*` stores.
2. **Trading engine** — `execution/oms.py` (`SingleWriterOMS`) serializes submit/cancel onto one queue; `LiveOMS`/`ShadowOMS` are pluggable backends; `execution/order_lifecycle.py` owns provisional → confirmed → terminal transitions.
3. **Strategy / risk engine** — `signals/*` and `strategies/*` produce `Intent`s; `risk/engine.py` evaluates them through a fixed gate sequence and emits `RiskDecision`s.

---

## 5. Package map (`src/tyrex_pm/`)

| Package | Role | Key files |
|---------|------|-----------|
| **core** | Shared dataclasses, ids, enums, time, errors, reason codes. | `models.py`, `enums.py`, `ids.py`, `reason_codes.py`, `events.py`, `bus.py` |
| **venue/polymarket** | Pure I/O adapter. CLOB bridge, REST clients (Gamma, Data API), WS, normalizers, auth, heartbeat. | `clob_bridge.py`, `clob_wallet_sync.py`, `clob_heartbeat.py`, `gamma_client.py`, `data_api_client.py`, `user_ws.py`, `market_ws.py`, `normalizers.py`, `auth.py`, `clob_env.py`, `positions_sync.py` |
| **state** | Internal truth: stores + reconcile. | `wallet_store.py`, `order_store.py`, `market_store.py` (`MarketStateStore`), `fill_state.py` (finality helper), `allocation_ledger.py`, `strategy_store.py`, `reconcile.py`, `shadow_wallet.py` |
| **ingestion** | Long-lived inputs and watermark-driven guru polling. | `guru_stream.py`, `user_stream.py`, `market_stream.py`, `historical_backfill.py` |
| **signals** | Reusable signal building blocks (no HTTP). Generic `Signal` protocol. | `base.py`, `guru_copy_signal.py`, `simple_signal.py`, `validation_signal.py` |
| **strategies** | Composition only — `on_signal` → intents. | `base.py` (`Strategy.on_signal`), `guru_follow/{strategy,filters,sizing,exits}.py`, `simple_signal_test/`, `validation_harness/`, `paired_binary/` (P4.6 design) |
| **risk** | Fail-closed `RiskEngine` + per-policy modules + planned-order validator. | `engine.py`, `planned_order.py`, `pretrade.py`, `deployment.py`, `capital.py`, `inventory.py`, `concurrency.py`, `health.py`, `kill_switch.py`, `venue_min_size.py`, `in_flight.py`, `evidence_format.py` |
| **execution** | Planner, OMS, order builder, lifecycle, cancel manager, slippage / liquidity guards. | `planner.py`, `models.py`, `oms.py`, `live_oms.py`, `adapters.py`, `order_builder.py`, `order_lifecycle.py`, `cancel_manager.py`, `router.py`, `slippage.py`, `liquidity_guard.py` |
| **protection** | Reusable TP/SL overlay (P4): registers after `allocation_buy_applied`, emits urgent `ExitIntent`s. | `config.py`, `registry.py`, `monitor.py`, `trigger_eval.py`, `sizing.py`, `lifecycle.py` |
| **runtime** | App entrypoint, config loading, coordinator, supervisors, modes. | `app.py`, `config.py`, `coordinator.py`, `pipeline.py`, `fixture_signal_run.py`, `validation_harness_run.py`, `validation_harness_live.py`, `finality_waiter.py`, `protection_supervisor.py`, `market_data_runtime.py`, `protection_runtime.py`, `paired_binary_run.py` (P4.6 design), `live_supervisor.py`, `supervisors.py`, `health_runtime.py`, `healthchecks.py`, `live_attest.py`, `risk_contexts.py`, `dependency_graph.py`, `modes.py` |
| **reporting** | Facts schema + sinks + summarizer. | `facts.py`, `schema_v2.py`, `oms_payload.py`, `summarize.py`, `sinks/jsonl.py` |

Per-module READMEs live under [modules/](modules/README.md).

---

## 6. Canonical data model (`core/models.py`)

```python
GuruTradeSignal(guru_wallet, token_id, side, size, price, notional_usd,
                dedup_key, ts_venue, raw_ref, conviction_score)

EnterIntent(token_id, side, size, limit_price, order_style, intent_id)
ExitIntent(...)        # SELL of an existing position
ReduceIntent(...)      # partial close
CancelIntent(venue_order_id, client_order_id, intent_id)
Intent = EnterIntent | ExitIntent | ReduceIntent | CancelIntent

ApprovedIntent(intent, client_order_id, run_id)
ApprovedCancel(venue_order_id, client_order_id, run_id, intent_id)

RiskDecision(approved: bool,
             reason_codes: tuple[str, ...],
             approved_intent: ApprovedIntent | None,
             detail: str | None,
             approved_cancel: ApprovedCancel | None,
             extensions: dict | None)   # operator-visible evidence

WalletPosition(token_id, qty, avg_price_usd)
OpenOrderView(token_id, side, remaining_size, limit_price,
              client_order_id, venue_order_id,
              original_size, size_matched,
              venue_state_source, order_status)
TradeFillRecord(token_id, side, size, price, status, ts_utc, source)

RiskContext(execution_mode, wallet_positions, open_orders,
            usdc_balance, usdc_allowance, last_wallet_sync_ts,
            mark_prices, kill_switch, health_ok, heartbeat_ok,
            clob_session_ok, in_flight_order_count,
            orders_in_flight_by_token, reconcile_drift,
            venue_truth_stale, in_flight_buy_reservations,
            # V2-native additions:
            first_v2_sync_complete,   # gates new-order risk eval until first venue truth rebuild
            market_info)              # {token_id: MarketInfo} per-market venue truth (tick, mos, neg-risk, fee, outcomes)
```

`RiskContext` is built by `RuntimeCoordinator.build_risk_context(app)` on every signal, so risk always sees the freshest store snapshot.

---

## 7. The `RiskEngine` gate sequence

`risk/engine.py::evaluate_intent` runs a deterministic ordered sequence (any failure returns immediately):

1. **Kill switch** — `kill_switch.check_kill_switch`.
2. **Cancel intents** — short-circuit; need `venue_order_id` or `client_order_id`.
3. **Concurrency** — `concurrency.check_concurrency` (max in-flight).
4. **Aggressive readiness** — wallet sync freshness, heartbeat, user WS, reconcile drift, plus the V2 first-sync gate `bootstrap_not_complete` (denies new-order intents in live mode until the first successful `refresh_wallet_from_clob` flips `HealthRuntime.first_v2_sync_complete`) — see `health.check_aggressive_readiness`.
5. **Notional** — min/max with `cap` or `deny` policy (`pretrade.apply_notional_min_max`). Always attaches the in-flight reservation totals to the decision.
6. **Deployment caps** — token + portfolio USD caps, including in-flight BUY reservations and mark requirement (`deployment.evaluate_deployment_caps`).
7. **Capital (BUY only)** — Polymarket USD balance + allowance net of in-flight reservations (`capital.evaluate_capital_buy`).
8. **Inventory (SELL/Reduce)** — venue position required when configured (`inventory.check_inventory_sell`).
9. **Venue minimum size** — venue's hard `min_order_size` (sourced from `MarketInfoCache` via `RiskContext.market_info` when live, falling back to `cfg.default_min_size` for shadow mode and tests); `deny` or `bump` (then re-validate gates 6 + 7) (`venue_min_size.evaluate_venue_min_size`). Evidence row records `venue_min_size_source = "venue" | "config_default"`.

Approved intents get a fresh `ClientOrderId` and become `ApprovedIntent`. The decision's `extensions` field carries operator-visible evidence (notional policy, deployment numbers, in-flight reservations, capital math, venue-min-size policy) and is merged into the `risk_decision` fact.

See [modules/risk/README.md](modules/risk/README.md) for per-policy detail.

---

## 8. Execution model

| Mode | Backend | Side effects |
|------|---------|--------------|
| **shadow** | `ShadowOMS` returns `"shadow_ack"`/`"shadow_cancel_ack"` immediately. | A synthetic fill is applied to `WalletStore` (`apply_local_shadow_fill=True`) so downstream risk sees the new position. |
| **live** | `LiveOMS` wraps `PyClobBridge` (sync `py-clob-client-v2.create_and_post_order` run on a thread). | Real submit; positions/balance update via user-WS + REST refresh loop; no synthetic fill. |

Both backends are wrapped by `SingleWriterOMS` so submits and cancels never overlap for the same wallet.

After a successful live ack the pipeline calls `refresh_wallet_coordinated_after_live_submit` to pull venue open orders one or two times, smoothing the REST-vs-WS race so the next signal sees the new resting order.

---

## 9. State stores

| Store | Owns | Mutated by |
|-------|------|------------|
| **WalletStore** | venue positions, USDC balance + allowance, merged `open_orders` (user WS primary, REST backstop), `_ws_cancel_tombstones`, `trade_fill_records`, `last_sync_ts`, `last_positions_sync_ts`. | `clob_wallet_sync.refresh_wallet_from_clob`, `positions_sync.refresh_positions_from_data_api`, `user_stream._apply_order_event`, `shadow_wallet.apply_*`. |
| **OrderStore** | local `LocalOrder` rows (`provisional` → `venue_confirmed`), `in_flight_by_token`, `pending_repair_fingerprints`, `terminal_audit`. | `execution.order_lifecycle.{register_submit,ack_submit,release_after_ack,apply_venue_open_order_to_local_orders,remove_local_resting_by_venue_order_id,sync_local_open_orders_from_venue_wallet}`. |
| **MarketStateStore** | order books / trades scaffolding (when market WS is enabled). | `ingestion.market_stream`. |
| **StrategyStore** | `guru_watermark` + `guru_seen_dedup` set. | `ingestion.guru_stream.ingest_guru_signals`. |

`reconcile.reconcile_open_orders(wallet, orders, **kw)` is the single function that compares local vs venue truth and produces a `ReconcileResult` with `drift_flags`, `blocking_drift_flags`, and `reconcile_severity`. The result drives `HealthRuntime.apply_reconcile` (which gates new orders) and is emitted as a `reconcile` fact (deduped by signature so unchanged states don't flood the log).

Full state-machine details: [LIVE_ARCHITECTURE.md](LIVE_ARCHITECTURE.md).

---

## 10. Reporting model

Every run writes to `var/reporting/runs/<run_id_or_name>/`:

```
manifest.json     # run_id, schema_version, git_sha, execution_mode, run_kind
facts.jsonl       # one fact per line; see reporting_fact_model.md
run_summary.json  # iteration counts, last guru_poll
```

Facts are deduped where it makes operational sense:
- `reconcile` facts are suppressed when the operator-relevant state tuple is unchanged (`pipeline._reconcile_signature`).
- `wallet_sync` facts are suppressed when balance/allowance/positions/open-order counts/marks are unchanged (timestamps are intentionally **excluded** from the dedup signature — see `pipeline._wallet_sync_signature`).

USD figures in evidence are quantized to 6 decimal places (`risk/evidence_format.py::s_usd`) so diffs across runs are stable.

Catalog: [reporting_fact_model.md](reporting_fact_model.md).

---

## 11. Configuration model (summary)

YAML files live under `config/` and merge in this order (later overrides earlier):

```
config/risk/default.yaml
config/runtime/default.yaml
config/strategies/<strategy>.yaml      # via --strategy
config/scenarios/<scenario>.yaml       # via --scenario (deep-merged into risk / runtime / strategy)
```

Secrets are **never** in YAML; they live in `.env` and are loaded by `runtime.app._maybe_load_dotenv` when `python-dotenv` is installed.

Authoritative reference: [CONFIG_MODEL.md](CONFIG_MODEL.md).

---

## 11.1 Order lifecycle vs allocation lifecycle

Four concepts must stay separate in runtime code and strategy state:

| Concept | Store / module | Meaning |
|---------|----------------|---------|
| **Submitted / resting order** | `OrderStore` | Venue accepted the order; it may have **zero** fill qty (`ack_status=live`). Not inventory. |
| **Fill evidence** | OMS match payload, user-WS `MATCHED`/`CONFIRMED`, `state/entry_fill_lifecycle.py` | Proves how many shares actually traded. |
| **Venue position** | `WalletStore.positions` | Exchange-ground-truth qty (REST/WS). Used for SELL inventory gate and repair. |
| **Owner allocation** | `AllocationLedger` | Strategy-attributed **sellable** qty. Credited only from fill evidence (matched OMS qty, WS `CONFIRMED`, shadow instant fill, repair). |

**Hard rules:**

- `maybe_apply_allocation_buy` credits **only** matched/filled qty (`allocation_buy_applied_from_fill`). Resting BUY → `order_resting_recorded` + `allocation_buy_skipped_unfilled_order`; ledger unchanged.
- Strategy **ACTIVE** transitions (e.g. `BOTH_LEGS_ACTIVE`) require fill/position evidence via `entry_qty_reconcile` + `entry_fill_lifecycle` — not ledger alone, not submit ack.
- Pending entry legs stay in `OrderStore`; strategies wait, cancel, or timeout-unwind.

See `state/entry_fill_lifecycle.py`, `runtime/entry_qty_reconcile.py`, `runtime/allocation_runtime.py`.

---

## 11.2 Reduce-only urgent exit risk policy

Increasing exposure still requires deployment marks. **Reducing** exposure must not trap on `deployment_mark_unknown`.

When `risk.exits.allow_reduce_only_mark_fallback=true` (default) and `validate_planned_order` sees an urgent reduce-only SELL denied for missing marks:

- Re-evaluates deployment using **executable bid** from planner book evidence (`mark_source=executable_bid`, `reason=reduce_only_exit_mark_fallback`).
- Still denies if: no venue position, no fresh bid, size exceeds venue/allocation clamp, not reduce-only, stale book (when `require_fresh_book_for_mark_fallback=true`).

Config:

```yaml
risk:
  exits:
    allow_reduce_only_mark_fallback: true
    require_fresh_book_for_mark_fallback: true
```

---

## 12. Where to read next

- **How risk decides:** [modules/risk/README.md](modules/risk/README.md)
- **Why a fact appeared (or didn't):** [reporting_fact_model.md](reporting_fact_model.md)
- **Why a venue order appeared "unmatched":** [LIVE_ARCHITECTURE.md](LIVE_ARCHITECTURE.md) §3 (reconcile)
- **How to add a new strategy:** [developer_guide.md](developer_guide.md) §4
- **How to run live for the first time:** [OPERATIONS.md](OPERATIONS.md)
