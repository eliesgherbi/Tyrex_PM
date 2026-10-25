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
        |                               (same signature as lab/)
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

Registered plugins: `ask70`, `feature_probe`, `settlement_value`, `z_gap`. `q_edge` is a guide mapping only. Session state lives in `MarketSessionRuntime` (`ZGapMarketRuntime` is a compatibility alias). Adding a plugin, a market family or a host enhancement: [extending.md](extending.md).

## v1: one contract, one pipeline, three clocks

Live, paper, observation and replay run the same host components. Only the source of data, the clock and the venue differ.

```text
Source (live feeds | warehouse | capture) -> feed router -> MarketSessionRuntime
   (books, reference, PTB seal, IndicatorGraph) -> decision_context() -> driver.evaluate()
   -> planner + final-gate checks -> venue -> outcomes -> driver.on_*_outcome()

            clock              venue
live        wall               Polymarket gateway
paper       wall               PaperVenue      ┐ same fill rule:
replay      recorded           replay Engine   ┘ execution/fill_model.py
```

| Concern | One place |
|---|---|
| Strategy contract | `strategies/protocol.py` (`StrategyDriver`), defaults in `strategies/base.py` (`BaseDriver`); hosts call it directly, never through `getattr` |
| Evaluation triggers | `strategies/triggers.py` (`feed`, `timer`, `replay`, `observe`) |
| Decision context | `runtime/decision_records.decision_context` (live, paper, observation, replay, lab); `DecisionContext.tau_s` is the time to close |
| Entry readiness | `IndicatorSpec.gates_entry` read by `indicators/readiness.py` |
| Fills and fees (simulated) | `execution/fill_model.py` |
| Book rebuild rule | the scenario's `book_reconstruction`; crossed books: `market_data/reconcile.is_crossed` |
| Market families | `config/markets/*.yaml` via `domain/polymarket/window_families.py` |
| Research offering | `StrategyPlugin.research` (`strategies/research.py`) |

The trading host is `runtime/trading_runtime.py` (fields, market-data callbacks, run loop, CLI entry points) composed from `runtime/host/`: `composition` (build), `readiness`, `metadata` (order-metadata warming), `window_binding` (rotation, restored sessions, summaries), `decisions` (evaluation and recording), `intents` (dispatcher, entries, exits, outcomes), `protection`, `deadlines`, `settlement_lifecycle` (hold commit and detach), `startup` (assessment and recovery) and `reporting`. A guard test keeps each component under 700 lines and the host under 800.

Replay of recorded data is `research/strategy_replay/run.py` (`run_window`) on the simulator `research/replay/engine.py`. Its release is `research/strategy_replay/engine_version.py` (`replay_v2`); experiments record it. `tools/v1_parity_check.py` re-runs saved workspace jobs and compares decision digests and economics.

## Ownership rules

- `facts/` owns the open fact catalog, `InputContract`, evaluation triggers, and eligibility facts. Strategies declare contracts; the runtime starts only the transitive source closure. See [strategy_inputs.md](strategy_inputs.md).
- `indicators/` owns reusable parameterized producers (OFI, imbalance, microprice, realized vol, EWMA) over a generic instrument-keyed L2/trade store. `MarketSessionRuntime` constructs one `IndicatorGraph` from the plugin’s `indicator_specs(config)` (else `InputContract.indicators`) and passes a `FeatureBundle` into `evaluate()`. `evaluate()` itself is strategy-neutral: books + `DecisionContext` + features + trigger/ids. PTB/Chainlink prints, when present, are `DecisionContext.references` (`ReferenceFacts`), not extra kwargs.
- `strategies/registry.py` owns `StrategyPlugin` registration. `run_config.py` loads kind via the registry; it has no `if strategy_kind == ...` branches.
- `runtime/scheduler.py` owns when `evaluate()` runs. Feed ingest never implies evaluation.
- `runtime/composition.py` owns which live adapters exist vs catalogued-but-unimplemented (Binance perp trades / L2 / funding). Spot L2 is live.
- `runtime/market_family.py` registers one runtime family (discovery of the current and next window) per configured window family in `config/markets/` (`btc_updown_5m` enabled; `btc_updown_15m` defined).
- `market_data/` owns normalized books, synchronization, freshness, and immutable views.
- `runtime/market_runtime.py` (`MarketSessionRuntime`) owns window preparation, optional PTB sealing / reference alignment as derived producers, and strategy-ready snapshots. PTB seal runs only when the contract requires `ptb.sealed`.
- `strategies/<name>/` owns only strategy calculations, intent decisions, and an `InputContract` plugin. Every driver inherits `BaseDriver` and implements the same `evaluate(market_snapshot, context, features, trigger, causation_id, correlation_id)` signature; execution feedback arrives through the contract's `on_entry_outcome` / `on_exit_outcome` / `on_hold_committed` methods. Last Chainlink / sealed K, when aligned-eval has them, live on `DecisionContext.references` (`ReferenceFacts`), not extra kwargs.
- `lab/` is the decision harness: `evaluate()` on synthetic `lab_evaluate_v1` packs with the shared context builder, no execution, no gateway. Recorded data is replayed (with simulated execution) only by `research/strategy_replay`. See [extending.md](extending.md).
- `protection/` owns `ProtectionSpec`, arming, and bid-mark triggers. See [protection.md](protection.md).
- `runtime/capabilities.py` owns readiness facts; the strategy cannot override them.
- `execution/account_state.py` owns continuous authenticated balance, allowance, token-inventory, and open-order observations. It publishes immutable `READY`, `INSUFFICIENT`, `UNAVAILABLE`, or `STALE` snapshots and never converts a failed read into a zero balance.
- `execution/planner.py` converts generic intents into side-correct typed order specifications (`TAKER` → FAK market buy; `MAKER` → post-only GTC, POSTed live). The host returns while the bid is `live` (`MAKER_LIMIT_POSTED`) until fill, cancel, or expire.
- `execution/coordinator.py` is the only submission authority and the only writer to execution state.
- `execution/reducer.py` derives state from durable normalized evidence.
- `execution/reconciliation.py` uses authenticated read-only SDK calls to resolve uncertainty.
- `execution/polymarket/gateway.py` owns one main-loop `AsyncSecureClient`, order preparation/POST, account reads, pagination, user-stream reconnect, and discarded SDK order-metadata warm (`warm_order_metadata`). Tick size comes from market-data (book/binding seed) with a one-shot public-book fetch only when local metadata is still missing.
- `persistence/execution_journal.py` is the durable source for replay and mutation-attempt audit.
- `persistence/run_evidence_journal.py` durably records explanatory readiness, decision, and runtime-error evidence; it never authorizes orders or derives positions.
- `reporting/run_report.py` is a read-only projection over market-data evidence, run evidence, and the execution aggregate.
- `runtime/trading_runtime.py` composes these modules through the host components in `runtime/host/`; it contains no venue-specific order semantics. It warms order metadata on bind, gates entry on `ORDER_METADATA_READY`, and drains intents via an event-woken serial dispatcher (not the 1 Hz market-data tick).

There are no numbered milestone runtimes, simulated execution backends, or fallback hosts in the supported graph.

## Concurrency model

One asyncio loop owns the authenticated client and an `AccountExecutionCoordinator` queue. HTTP responses, user-stream evidence, and REST reconciliation all enter that queue. No callback mutates the session directly and no async client is shared across event loops.

Account state for the active and prepared-next markets is refreshed in background tasks on that same loop. Market promotion reads the latest snapshot without performing sequential network calls. Only the runtime applies snapshots to capabilities, so background refresh cannot mutate execution readiness concurrently. During coordinator submit, account refresh for that market is held so a slow overlapping REST cycle cannot flip `entry_executable` mid prepare/final-gate.

Account-state refresh stays single-flight on the execution SDK client. Calls are individually sequenced through an active-market-priority gate, so a prepared-next refresh cannot monopolize that client after promotion. Startup and monitoring inspection uses a second SDK client and does not take the execution slot. Collateral, token, and open-order reads use the SDK transport's own timeouts; Tyrex does not force-cancel HTTP/2 calls from an outer timeout. Per-operation network time, queue wait, total refresh time, and refresh-cycle overruns are evidence rather than synthetic zero balances. Public REST book bootstrap/recovery runs off the asyncio loop and is coalesced per binding, so Wi-Fi latency cannot stop WebSocket or execution processing.

## Time, clocks, and strategy eligibility

Clock synchronization continuously retains the lowest-latency fresh observation from a rolling sample window. A synchronization measurement can exist without satisfying the strategy's uncertainty policy; reports therefore expose `sync_status`, `ready`, `reason_code`, round-trip time, offset, and uncertainty separately.

Three **schedulers** share the loop and must not be collapsed:

1. **Strategy evaluation** — `EvaluationScheduler` from the plugin `InputContract`.
2. **Protection** — book-driven `on_public_fact`; independent of whether `evaluate()` ran.
3. **Execution** — event-woken serial intent dispatcher (not the 1 Hz market-data tick).

The runtime also keeps three **readiness** concepts distinct:

1. `execution_infrastructure_ready`: account, stream, book, allowance, order-metadata warm, scope, and entry-window machinery can submit safely.
2. `strategy_inputs_eligible`: the driver's `EligibilityFacts` for **this** contract (z_gap: clock, exact PTB, model, basis, time bands; ask70 / feature_probe: books/clock/account eligibility — no PTB). The host reads `result.eligibility`, not `strategy_kind`.
3. economic signal: an eligible strategy evaluation selected an entry after fees and thresholds.

`entry_executable` requires both infrastructure readiness and model readiness. A volatility or PTB lockout therefore blocks entry without being classified as an infrastructure failure. Missing Binance on an ask70 run is not an infrastructure failure because that adapter is not started.

Durations and deadlines use monotonic or corrected-authoritative time as appropriate. Raw wall-clock timestamps are retained for operator correlation but are never subtracted from corrected timestamps.

## Safety model

Order signing is preparation, not mutation. The hot path is:

`plan -> create/sign -> capture newest BookView -> final gate -> durable dispatch authorization -> post_order -> normalize evidence`

The gateway adapts continuous strategy protection prices to the authoritative Polymarket tick without weakening them (BUY floor, SELL ceiling). Tick size is seeded from discovery into the book store (and preserved across WS snapshots that omit it); preparation falls back to one public book fetch only if local metadata is still missing. The final gate verifies binding, token, tick (when present on the book), monotonic candidate freshness, synchronized books, executable price, and depth after signing. A known preparation or gate rejection is durably `COMPLETED_NO_DISPATCH`; a process crash after authorization is recoverable because the durable record precedes POST.

Read failures are safe to retry because they cannot mutate the venue. A transport failure or task cancellation after `post_order` starts is different: it is durably classified as an uncertain dispatch and must be reconciled before another mutation.

Startup inspection is a read path plus local evidence. It does not sign or submit. Background readiness reads the local assessment deadline on the strategy tick and does not add an account-wide network call to `evaluate()`. An expired or contradictory assessment closes new BUYs. A protective exit on the active market stays available.

## Position protection

Exits are three orthogonal planes that share the same intent → planner → lifecycle sell spine:

1. **Strategy policy** — thesis / rich / time (z_gap) or entry-only harness policies (ask70; `feature_probe` when `emit_entry: true`).
2. **Risk / safety** — kill switch, mandatory flatten, crash recovery, manual deadline.
3. **Protection overlay** — framework-owned SL / reactive TP / trailing armed after confirmed fill from an explicit run-config `protection:` block (`ProtectionSpec`).

Strategies declare thresholds; the framework arms on confirmed exposure, evaluates the held token **best bid**, and emits `ExitIntent` / `FlattenIntent` with `PROTECTION_*` reason codes. Gateway “protection price” (tick-adapted limit) is unrelated to `ProtectionSpec`.

The `ask70` harness and `feature_probe` evaluate from active-window books and do not require a sealed PTB EXACT candidate. Z-Gap still requires sealed PTB before model evaluation. `feature_probe` requires a `protection:` block only when `emit_entry: true`. Full overlay contract: [protection.md](protection.md).

Precedence (high → low): emergency/kill → mandatory flatten / manual deadline → hard SL → strategy exits → trailing → take-profit.

## Reporting authority

Market-data reporting owns only public-feed, book-health, clock, discovery, seal, and rollover evidence. It does not claim whether authentication, an OMS, or a venue mutation occurred.

The unified `run_summary.json` derives mutation counts and order timelines exclusively from `execution_events`. Readiness transitions, strategy decisions, skipped evaluations, and runtime errors are appended to `run_events` in the same SQLite database. Reporting never feeds back into trading state.

Normal and failed runs write `run_summary.json` atomically. Identity (`reproducibility`) is captured before runtime/network creation. If full projection fails, the outer runtime writes a primitive emergency report containing the fatal error, the journal-derived mutation count, and the already-captured identity/hashes (not the full effective-config dump).

Tick, intent consume, and the final BUY gate share the runtime `TimeAuthority.now_corrected_utc` callable. The BUY gate also applies a contract-driven `features_ready` check (z_gap EWMA); SELL and protective flatten do not.

A run with no execution session includes `no_entry_diagnosis`. It is successful when execution infrastructure was ready, at least one strategy decision had fully eligible inputs, and the strategy selected no economic entry. It is also successful as `NO_ENTRY_CUTOFF_BLOCKED` when the only entry intent was rejected at the hard BUY cutoff, with no session and no mutation. That `ok: true` means a safe shutdown, not a confirmed entry and exit. `NO_ENTRY_RUNTIME_BLOCKED` and `NO_ENTRY_STRATEGY_INPUT_BLOCKED` are explicit non-success outcomes. Every strategy decision retains a deterministic primary reason plus all simultaneous input blockers. The strategy entry window closes `entry_dispatch_margin_s` before the hard cutoff. Consume, the final BUY gate, and POST still enforce the hard cutoff.

## Not implemented in this release

These exist as types, catalog entries, or recorded intents — they are not live product behavior:

- `q_edge` strategy plugin (guide contract only)
- Binance perp trades/L2 and funding adapters (spot L2 + derived mid are live)
- Automatic on-chain redeem / merge (hold-to-resolution itself runs: settlement ledger, payout proof, operator redemption worklist)
- Window families for assets other than BTC (collector and runtime feeds take BTC symbols only) and multi-outcome or windowless markets
- Tune (parameter search) in the research workspace

