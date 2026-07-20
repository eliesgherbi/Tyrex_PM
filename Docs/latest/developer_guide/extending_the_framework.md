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

## Strategy callbacks available today

Implement `on_start`, `on_signal`, `on_stop` only.  
Do **not** assume `on_timer` or `on_execution_event` exist.

Protocol types `on_signal` as returning `tuple[StrategyDecision, list[IntentLike]]` where `IntentLike` is `EnterIntent | ExitIntent | FlattenIntent`.
Use F1 actions (`WAIT`/`SKIP`/`ENTER`/`HOLD`/`EXIT`/`FLATTEN`/`BLOCKED`) and put exit-family distinctions in `reason_code`. Do not invent `HOLD_TO_RESOLUTION` as a generic action.

## Add an indicator

1. Implement under `tyrex_pm/indicators/`  
2. Consume store snapshots / typed inputs only  
3. Keep side-effect free; add unit tests  

## Add a signal

1. Package under `tyrex_pm/signals/`  
2. Do not emit orders or call transports  

## Add a strategy

1. Implement the `Strategy` protocol  
2. Emit intents + evidence only  
3. Do **not** import `tyrex_pm.execution.polymarket.*` or `runtime/r7*`  
4. Validate under OBSERVE → SHADOW before any live scope  
5. Use `ReferenceMomentumStrategy` as a **structural** example only  

## Add a risk rule

1. Extend `RiskEngine` / policies / `RiskReason`  
2. Keep fail-closed defaults  
3. Unit-test approve/deny per mode  

## Add an adapter

1. Place under `tyrex_pm/adapters/<venue>/`  
2. Normalize to framework events/snapshots  
3. Provide fixture source for offline tests  

## Add an execution planner

1. Keep pricing/qty/order-type in planning modules  
2. Entry and exit planners stay separate  
3. Live exit planning must use fresh side-correct books  

## Z-Gap

Future work only — [`../../specifications/09_z_gap_future_mapping.md`](../../specifications/09_z_gap_future_mapping.md).  
Do not document unimplemented Z-Gap contracts as existing. Do not import R7-specific runtime packages from Z-Gap.
