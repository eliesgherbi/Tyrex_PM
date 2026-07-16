# 04 — Event and runtime flow

**Phase:** R2 implements the dispatcher; this document defines the accepted design.

## Event vs signal vs intent vs command

| Kind | Meaning |
|------|---------|
| Event | Immutable fact that happened |
| Signal | Typed market interpretation (not an order) |
| Intent | Strategy economic request |
| Command / plan | Approved execution instruction after risk |

## Initial event taxonomy

`BookUpdated`, `ReferencePriceUpdated`, `TimerElapsed`, optional `WindowOpened`/`WindowClosed`, execution family (`OrderAccepted`, `OrderRejected`, `OrderPartiallyFilled`, `OrderFilled`, `OrderCanceled`, `PositionChanged`), `KillSwitchActivated`.

Every event carries `event_id`, timestamps, `correlation_id`, optional `causation_id`, `source`.

## Dispatch

- In-process `EventDispatcher.subscribe` / `publish`.
- Deterministic handler order per event type.
- Synchronous by default; async only at I/O edges.
- Venue order/fill handlers must be idempotent.

## Modes

| Mode | Execution dispatch |
|------|--------------------|
| observe | No OMS; facts may record hypothetical intents |
| shadow | Shadow OMS |
| live-tiny | Live Polymarket OMS (authorization required) |

Mode must not change indicator/signal mathematics.

## Failure behavior (target)

Handler errors are logged/facted; fail-closed risk denies intents. Exact policy implemented in R2–R4 tests.
