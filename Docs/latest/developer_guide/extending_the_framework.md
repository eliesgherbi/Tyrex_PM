# Extending the framework

**Purpose:** add capabilities without breaking ownership boundaries.

## Extension flow

```text
Input contract
→ Indicator
→ Signal
→ Strategy intent
→ Shared risk
→ Shared execution
```

A strategy **must not** reimplement venue APIs, book reconstruction, risk authorization, execution pricing, OMS state, portfolio truth, or persistence.

## Add an indicator

1. Implement under `tyrex_pm/indicators/`
2. Consume store snapshots / typed inputs only
3. Keep it reusable and side-effect free
4. Wire into the host/signal path with tests

## Add a signal

1. Package indicator outputs under `tyrex_pm/signals/`
2. Do not emit orders or call transports
3. Cover construction with unit tests

## Add a strategy

1. Implement the `Strategy` protocol (`on_start` / `on_signal` / `on_stop`)
2. Emit `EnterIntent` / `ExitIntent` / evidence only
3. Do **not** import `tyrex_pm.execution.polymarket.*`
4. Validate under OBSERVE → SHADOW before any live scope
5. Use `ReferenceMomentumStrategy` as a **structural** example, not a trading recommendation

## Add a risk rule

1. Extend `RiskEngine` / policies / `RiskReason`
2. Keep fail-closed defaults
3. Add unit tests for approve/deny matrices per mode

## Add an adapter

1. Place under `tyrex_pm/adapters/<venue>/`
2. Normalize to framework events/snapshots
3. No strategy-facing HTTP clients
4. Fixture source required for offline tests

## Add an execution planner

1. Keep pricing/qty/order-type in planning / execution planning modules
2. Entry and exit planners stay separate (BUY ceiling ≠ SELL floor)
3. Live exit planning must use fresh side-correct books

## Z-Gap

Z-Gap is **future work** only. See [`../../specifications/09_z_gap_future_mapping.md`](../../specifications/09_z_gap_future_mapping.md).  
Do not document unimplemented Z-Gap contracts as existing APIs.
