# Unified trading runtime

## Objective

The framework should let a developer add strategy-specific model, policy, state, and configuration while reusing market ingestion, readiness, execution, reconciliation, persistence, and reporting.

```text
YAML + StrategyPlugin.input_contract
        |
        v
composition: start only source-fact adapters in the contract closure
        |
        v
Public feeds -> stores (ingest always) -> EvaluationScheduler
        |                                      |
        |                                      v  (declared OnFact / OnTimer only)
        |                               Strategy driver.evaluate
        |                                      |
        |                               typed intent
        |                                      v
Account-state authority -> capabilities -> execution lifecycle -> async gateway
                                      |                    |
                                      v                    v
                              SQLite event journal   Polymarket SDK
                                      |
                                      v
                               terminal run report

Book updates also wake the protection overlay (independent of evaluate()).
```

Registered live plugins: `z_gap` and `ask70`. `q_edge` is a guide mapping only. Session state lives in `MarketSessionRuntime` (`ZGapMarketRuntime` is a compatibility alias).

## Ownership rules

- `facts/` owns the open fact catalog, `InputContract`, evaluation triggers, and eligibility facts. Strategies declare contracts; the runtime starts only the transitive source closure. See [strategy_inputs.md](strategy_inputs.md).
- `indicators/` owns reusable parameterized producers (OFI, imbalance, microprice, realized vol, EWMA) over a generic instrument-keyed L2/trade store.
- `strategies/registry.py` owns `StrategyPlugin` registration. `run_config.py` loads kind via the registry; it has no `if strategy_kind == ...` branches.
- `runtime/scheduler.py` owns when `evaluate()` runs. Feed ingest never implies evaluation.
- `runtime/composition.py` owns which live adapters exist vs catalogued-but-unimplemented (Binance L2 / perp / funding).
- `runtime/market_family.py` owns discovery/window model per family (`btc_updown_5m` today).
- `market_data/` owns normalized books, synchronization, freshness, and immutable views.
- `runtime/market_runtime.py` (`MarketSessionRuntime`) owns window preparation, optional PTB sealing / reference alignment as derived producers, and strategy-ready snapshots. PTB seal runs only when the contract requires `ptb.sealed`.
- `strategies/<name>/` owns only strategy calculations, intent decisions, and an `InputContract` plugin.
- `protection/` owns `ProtectionSpec`, arming, and bid-mark triggers. See [protection.md](protection.md).
- `runtime/capabilities.py` owns readiness facts; the strategy cannot override them.
- `execution/account_state.py` owns continuous authenticated balance, allowance, token-inventory, and open-order observations. It publishes immutable `READY`, `INSUFFICIENT`, `UNAVAILABLE`, or `STALE` snapshots and never converts a failed read into a zero balance.
- `execution/planner.py` converts generic intents into side-correct typed order specifications (`TAKER` → FAK market buy; `MAKER` → GTC limit, not POSTed live).
- `execution/coordinator.py` is the only submission authority and the only writer to execution state.
- `execution/reducer.py` derives state from durable normalized evidence.
- `execution/reconciliation.py` uses authenticated read-only SDK calls to resolve uncertainty.
- `execution/polymarket/gateway.py` owns one main-loop `AsyncSecureClient`, order preparation/POST, account reads, pagination, user-stream reconnect, and discarded SDK order-metadata warm (`warm_order_metadata`). Tick size comes from market-data (book/binding seed) with a one-shot public-book fetch only when local metadata is still missing.
- `persistence/execution_journal.py` is the durable source for replay and mutation-attempt audit.
- `persistence/run_evidence_journal.py` durably records explanatory readiness, decision, and runtime-error evidence; it never authorizes orders or derives positions.
- `reporting/run_report.py` is a read-only projection over market-data evidence, run evidence, and the execution aggregate.
- `runtime/trading_runtime.py` composes these modules; it contains no venue-specific order semantics. It warms order metadata on bind, gates entry on `ORDER_METADATA_READY`, and drains intents via an event-woken serial dispatcher (not the 1 Hz market-data tick).

There are no numbered milestone runtimes, simulated execution backends, or fallback hosts in the supported graph.

## Concurrency model

One asyncio loop owns the authenticated client and an `AccountExecutionCoordinator` queue. HTTP responses, user-stream evidence, and REST reconciliation all enter that queue. No callback mutates the session directly and no async client is shared across event loops.

Account state for the active and prepared-next markets is refreshed in background tasks on that same loop. Market promotion reads the latest snapshot without performing sequential network calls. Only the runtime applies snapshots to capabilities, so background refresh cannot mutate execution readiness concurrently. During coordinator submit, account refresh for that market is held so a slow overlapping REST cycle cannot flip `entry_executable` mid prepare/final-gate.

Private account observations are single-flight on the shared SDK client. Calls are individually sequenced through an active-market-priority gate, so a prepared-next refresh cannot monopolize the client after promotion. Collateral, token, and open-order reads use the SDK transport's own timeouts; Tyrex does not force-cancel HTTP/2 calls from an outer timeout. Per-operation network time, queue wait, total refresh time, and refresh-cycle overruns are evidence rather than synthetic zero balances. Public REST book bootstrap/recovery runs off the asyncio loop and is coalesced per binding, so Wi-Fi latency cannot stop WebSocket or execution processing.

## Time, clocks, and strategy eligibility

Clock synchronization continuously retains the lowest-latency fresh observation from a rolling sample window. A synchronization measurement can exist without satisfying the strategy's uncertainty policy; reports therefore expose `sync_status`, `ready`, `reason_code`, round-trip time, offset, and uncertainty separately.

Three **schedulers** share the loop and must not be collapsed:

1. **Strategy evaluation** — `EvaluationScheduler` from the plugin `InputContract`.
2. **Protection** — book-driven `on_public_fact`; independent of whether `evaluate()` ran.
3. **Execution** — event-woken serial intent dispatcher (not the 1 Hz market-data tick).

The runtime also keeps three **readiness** concepts distinct:

1. `execution_infrastructure_ready`: account, stream, book, allowance, order-metadata warm, scope, and entry-window machinery can submit safely.
2. `strategy_inputs_eligible`: the driver's `EligibilityFacts` for **this** contract (z_gap: clock, exact PTB, model, basis, time bands; ask70: books/clock/account eligibility — no PTB). The host reads `result.eligibility`, not `strategy_kind`.
3. economic signal: an eligible strategy evaluation selected an entry after fees and thresholds.

`entry_executable` requires both infrastructure readiness and model readiness. A volatility or PTB lockout therefore blocks entry without being classified as an infrastructure failure. Missing Binance on an ask70 run is not an infrastructure failure because that adapter is not started.

Durations and deadlines use monotonic or corrected-authoritative time as appropriate. Raw wall-clock timestamps are retained for operator correlation but are never subtracted from corrected timestamps.

## Safety model

Order signing is preparation, not mutation. The hot path is:

`plan -> create/sign -> capture newest BookView -> final gate -> durable dispatch authorization -> post_order -> normalize evidence`

The gateway adapts continuous strategy protection prices to the authoritative Polymarket tick without weakening them (BUY floor, SELL ceiling). Tick size is seeded from discovery into the book store (and preserved across WS snapshots that omit it); preparation falls back to one public book fetch only if local metadata is still missing. The final gate verifies binding, token, tick (when present on the book), monotonic candidate freshness, synchronized books, executable price, and depth after signing. A known preparation or gate rejection is durably `COMPLETED_NO_DISPATCH`; a process crash after authorization is recoverable because the durable record precedes POST.

Read failures are safe to retry because they cannot mutate the venue. A transport failure or task cancellation after `post_order` starts is different: it is durably classified as an uncertain dispatch and must be reconciled before another mutation.

## Position protection

Exits are three orthogonal planes that share the same intent → planner → lifecycle sell spine:

1. **Strategy policy** — thesis / rich / time (z_gap) or entry-only harness policies (ask70).
2. **Risk / safety** — kill switch, mandatory flatten, crash recovery, manual deadline.
3. **Protection overlay** — framework-owned SL / reactive TP / trailing armed after confirmed fill from an explicit run-config `protection:` block (`ProtectionSpec`).

Strategies declare thresholds; the framework arms on confirmed exposure, evaluates the held token **best bid**, and emits `ExitIntent` / `FlattenIntent` with `PROTECTION_*` reason codes. Gateway “protection price” (tick-adapted limit) is unrelated to `ProtectionSpec`.

The `ask70` harness evaluates from active-window books and does not require a sealed PTB EXACT candidate. Z-Gap still requires sealed PTB before model evaluation. Full overlay contract: [protection.md](protection.md).

Precedence (high → low): emergency/kill → mandatory flatten / manual deadline → hard SL → strategy exits → trailing → take-profit.

## Reporting authority

Market-data reporting owns only public-feed, book-health, clock, discovery, seal, and rollover evidence. It does not claim whether authentication, an OMS, or a venue mutation occurred.

The unified `run_summary.json` derives mutation counts and order timelines exclusively from `execution_events`. Readiness transitions, strategy decisions, skipped evaluations, and runtime errors are appended to `run_events` in the same SQLite database. Reporting never feeds back into trading state.

Normal and failed runs write `run_summary.json` atomically. If full projection fails, the outer runtime writes a primitive emergency report containing the fatal error and the journal-derived mutation count.

A run with no execution session includes `no_entry_diagnosis`. It is successful only when execution infrastructure was ready, at least one strategy decision had fully eligible inputs, and the strategy selected no economic entry. `NO_ENTRY_RUNTIME_BLOCKED` and `NO_ENTRY_STRATEGY_INPUT_BLOCKED` are explicit non-success outcomes. Every strategy decision retains a deterministic primary reason plus all simultaneous input blockers.

## Not implemented in this release

These exist as types, catalog entries, or recorded intents — they are not live product behavior:

- `q_edge` strategy plugin
- Binance spot L2, perp trades/L2, and funding adapters
- Live POST of `LiquidityRole.MAKER` GTC limits (`MAKER_LIMIT_PLANNED` only)
- Resting GTC take-profit (`reactive_fak` only)
- Automatic on-chain redeem / merge / hold-to-resolution execution
- Market families other than `btc_updown_5m`

