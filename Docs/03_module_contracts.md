# 03 — Module contracts

**Phase:** R2 — `core` and `engine` implemented.

## `tyrex_pm.application` (R1)

CLI entry (`tyrex-pm version|help`). No venue I/O.

## `tyrex_pm.core` (R2)

| Module | Contract |
|--------|----------|
| `ids` | `EventId`, `CorrelationId`, `InstrumentId`, `MarketId`, `TokenId`, `StrategyId`, `RunId` |
| `clock` | `Clock`, `SystemClock`, `FakeClock`, `require_utc` |
| `numerics` | Decimal helpers; reject float; Polymarket price ∈ [0,1] |
| `instruments` | `Instrument`, `OutcomeSide` |
| `snapshots` | `BookLevel`, `BookSnapshot` (full book), `ReferencePriceSnapshot` |
| `events` | `Event`, `BookUpdated`, `ReferencePriceUpdated`, `TimerElapsed`, `EventSource` |
| `indicators` | `IndicatorResult` envelope |
| `signals` | `Signal` envelope (DirectionalSignal in R3) |
| `facts` | `FactEnvelope` (JSONL sink in R3) |

**Forbidden deps:** `engine`, adapters, strategies, `old`, NautilusTrader.

**Deferred:** Intent hierarchy (R4), execution events (R5), WindowOpened/Closed (only if R3 scheduler needs them).

## `tyrex_pm.engine` (R2)

| Type | Contract |
|------|----------|
| `EventDispatcher` | subscribe / unsubscribe / publish |
| `Subscription` | opaque handle |
| `DispatchResult` | handler_count, delivered, queued_followups |
| `DispatchError` | fail-fast handler failure |

**Allowed deps:** `tyrex_pm.core` only.  
**Forbidden:** adapters, strategies, portfolio, `old`.

## Planned later (not created)

`adapters`, `market_data`, `indicators` (calc), `strategies`, `risk`, `execution`, `portfolio`, `lifecycle`, `operations`, `persistence`, `reporting`, `domain.polymarket` — when consumers exist.
