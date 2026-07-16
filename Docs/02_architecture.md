# 02 — Architecture and ownership

**Phase:** R1 documents the target ownership model. Packages beyond `application/` appear when R2+ adds consumers.

## Engine decision

Tyrex_PM implements its **own** minimal in-process event-driven engine. NautilusTrader is not used.

## Target flow

```text
Adapter → Event → Dispatcher → State owner → Indicator → Signal
  → Strategy → Intent → Risk → Plan/Command → OMS
  → Execution events → Portfolio → Strategy feedback → Facts
```

## State ownership

| State | Authoritative owner |
|-------|---------------------|
| Instruments | Instrument registry |
| Polymarket books | Market-state store |
| Binance reference | Reference-data store |
| Indicators | Indicator instances |
| Signals | Immutable messages |
| Orders | Order store |
| Fills | Order store or fill ledger (choose in R5) |
| Positions | Portfolio |
| Strategy lifecycle | Strategy host |
| Runtime mode | Application configuration |
| Kill switch | Operations / risk |
| Persistence | State repository |
| Facts | Reporting sink |

The event dispatcher transports information; it is not a second state store.

## Dependency direction

`application → strategies/operations → indicators/signals/market_data/domain → engine interfaces → adapters`

Forbidden: strategy → concrete venue clients; adapters → strategy decisions; active code → `old/`.

## Active tree (R1)

Only `src/tyrex_pm/application` (+ package root) exists. Historical code: `old/`.
