# Unified trading runtime

## Objective

The framework should let a developer add strategy-specific model, policy, state, and configuration while reusing market ingestion, readiness, execution, reconciliation, persistence, and reporting.

```text
Public feeds -> normalized market state -> immutable decision snapshot
                                            |
                                            v
                                      Strategy driver
                                            |
                                      typed intent
                                            |
                                            v
Account-state authority -> capabilities -> execution lifecycle -> async gateway
                                      |                    |
                                      v                    v
                              SQLite event journal   Polymarket SDK
                                      |
                                      v
                               terminal run report
```

## Ownership rules

- `market_data/` owns normalized books, synchronization, freshness, and immutable views.
- `runtime/market_runtime.py` owns window preparation, PTB sealing, reference alignment, and strategy-ready snapshots.
- `strategies/<name>/` owns only strategy calculations and intent decisions.
- `runtime/capabilities.py` owns readiness facts; the strategy cannot override them.
- `execution/account_state.py` owns continuous authenticated balance, allowance, token-inventory, and open-order observations. It publishes immutable `READY`, `INSUFFICIENT`, `UNAVAILABLE`, or `STALE` snapshots and never converts a failed read into a zero balance.
- `execution/planner.py` converts generic intents into side-correct typed order specifications.
- `execution/coordinator.py` is the only submission authority and the only writer to execution state.
- `execution/reducer.py` derives state from durable normalized evidence.
- `execution/reconciliation.py` uses authenticated read-only SDK calls to resolve uncertainty.
- `execution/polymarket/gateway.py` owns one main-loop `AsyncSecureClient`, order preparation/POST, account reads, pagination, and user-stream reconnect. Tick size comes from market-data (book/binding seed) with a one-shot public-book fetch only when local metadata is still missing.
- `persistence/execution_journal.py` is the durable source for replay and mutation-attempt audit.
- `persistence/run_evidence_journal.py` durably records explanatory readiness, decision, and runtime-error evidence; it never authorizes orders or derives positions.
- `reporting/run_report.py` is a read-only projection over market-data evidence, run evidence, and the execution aggregate.
- `runtime/trading_runtime.py` composes these modules; it contains no venue-specific order semantics.

There are no numbered milestone runtimes, simulated execution backends, or fallback hosts in the supported graph.

## Concurrency model

One asyncio loop owns the authenticated client and an `AccountExecutionCoordinator` queue. HTTP responses, user-stream evidence, and REST reconciliation all enter that queue. No callback mutates the session directly and no async client is shared across event loops.

Account state for the active and prepared-next markets is refreshed in background tasks on that same loop. Market promotion reads the latest snapshot without performing sequential network calls. Only the runtime applies snapshots to capabilities, so background refresh cannot mutate execution readiness concurrently.

Private account observations are single-flight on the shared SDK client. Calls are individually sequenced through an active-market-priority gate, so a prepared-next refresh cannot monopolize the client after promotion. Collateral, token, and open-order reads use the SDK transport's own timeouts; Tyrex does not force-cancel HTTP/2 calls from an outer timeout. Per-operation network time, queue wait, total refresh time, and refresh-cycle overruns are evidence rather than synthetic zero balances. Public REST book bootstrap/recovery runs off the asyncio loop and is coalesced per binding, so Wi-Fi latency cannot stop WebSocket or execution processing.

## Time and strategy eligibility

Clock synchronization continuously retains the lowest-latency fresh observation from a rolling sample window. A synchronization measurement can exist without satisfying the strategy's uncertainty policy; reports therefore expose `sync_status`, `ready`, `reason_code`, round-trip time, offset, and uncertainty separately.

The runtime keeps three concepts distinct:

1. `execution_infrastructure_ready`: account, stream, book, allowance, scope, and entry-window machinery can submit safely.
2. `strategy_inputs_eligible`: clock, exact PTB, model, basis, and strategy time bands are valid for a decision.
3. economic signal: an eligible strategy evaluation selected an entry after fees and thresholds.

`entry_executable` requires both infrastructure readiness and model readiness. A volatility or PTB lockout therefore blocks entry without being classified as an infrastructure failure.

Durations and deadlines use monotonic or corrected-authoritative time as appropriate. Raw wall-clock timestamps are retained for operator correlation but are never subtracted from corrected timestamps.

## Safety model

Order signing is preparation, not mutation. The hot path is:

`plan -> create/sign -> capture newest BookView -> final gate -> durable dispatch authorization -> post_order -> normalize evidence`

The gateway adapts continuous strategy protection prices to the authoritative Polymarket tick without weakening them (BUY floor, SELL ceiling). Tick size is seeded from discovery into the book store (and preserved across WS snapshots that omit it); preparation falls back to one public book fetch only if local metadata is still missing. The final gate verifies binding, token, tick (when present on the book), monotonic candidate freshness, synchronized books, executable price, and depth after signing. A known preparation or gate rejection is durably `COMPLETED_NO_DISPATCH`; a process crash after authorization is recoverable because the durable record precedes POST.

Read failures are safe to retry because they cannot mutate the venue. A transport failure or task cancellation after `post_order` starts is different: it is durably classified as an uncertain dispatch and must be reconciled before another mutation.

## Reporting authority

Market-data reporting owns only public-feed, book-health, clock, discovery, seal, and rollover evidence. It does not claim whether authentication, an OMS, or a venue mutation occurred.

The unified `run_summary.json` derives mutation counts and order timelines exclusively from `execution_events`. Readiness transitions, strategy decisions, skipped evaluations, and runtime errors are appended to `run_events` in the same SQLite database. Reporting never feeds back into trading state.

Normal and failed runs write `run_summary.json` atomically. If full projection fails, the outer runtime writes a primitive emergency report containing the fatal error and the journal-derived mutation count.

A run with no execution session includes `no_entry_diagnosis`. It is successful only when execution infrastructure was ready, at least one strategy decision had fully eligible inputs, and the strategy selected no economic entry. `NO_ENTRY_RUNTIME_BLOCKED` and `NO_ENTRY_STRATEGY_INPUT_BLOCKED` are explicit non-success outcomes. Every strategy decision retains a deterministic primary reason plus all simultaneous input blockers.
