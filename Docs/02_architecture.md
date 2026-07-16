# 02 — Architecture and ownership

**Phase:** R2 implemented for `core/` + `engine/`  
**Engine:** Minimal Tyrex in-process event-driven engine (no NautilusTrader)

## Implemented flow (R2 primitives)

```text
(future Adapter)
    → typed Event (BookUpdated | ReferencePriceUpdated | TimerElapsed)
    → EventDispatcher.publish
    → (future state owner / indicator / signal / strategy)
```

R2 provides the contracts and dispatcher only. Adapters and strategies begin in R3+.

## Packages present

| Package | Role |
|---------|------|
| `tyrex_pm.application` | CLI (R1) |
| `tyrex_pm.core` | Ids, clock, events, snapshots, envelopes |
| `tyrex_pm.engine` | `EventDispatcher` |

## State ownership (target; stores arrive R3+)

| State | Authoritative owner |
|-------|---------------------|
| Instruments | Instrument registry (R3+) |
| Polymarket books | Market-state store (R3) |
| Binance reference | Reference-data store (R3) |
| Indicators | Indicator instances (R3) |
| Signals | Immutable messages |
| Orders / fills / positions | Portfolio path (R5) |
| Facts | Reporting sink (R3+) |

The dispatcher transports events; it is **not** a state store and does **not** deduplicate venue events.

## Dependency direction

`application → (future strategies) → core ← engine`  

`engine` may import `core`. `core` must not import `engine`, adapters, or strategies. Nothing imports `old/`.

## Polymarket identity mapping

| Concept | Type |
|---------|------|
| Market / condition | `MarketId` |
| Tradable CLOB token | `TokenId` |
| Framework instrument key | `InstrumentId` (typically token string) |
| YES / NO | `OutcomeSide` on `Instrument` |
| Window/slug | Not a core ID — scheduler concern in R3 |

## Numeric policy

Trading prices/quantities use `Decimal`. Floats rejected at construction. Venue tick rounding deferred to adapter/execution (R3–R6).
