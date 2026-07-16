# 03 — Module contracts

**Phase:** R4

## Strategy

Protocol: `on_start`, `on_signal`/`apply_transition`, `on_stop`.  
Must not import adapters, risk, planner, OMS, or portfolio.

`ReferenceMomentumStrategy.evaluate` — R3 observe decision (unchanged).  
Transition policy emits at most one `EnterIntent` per eligible direction change.

## Intents (`tyrex_pm.core.intents`)

`EnterIntent`: BUY, `target_notional`, instrument/market, evidence, `semantic_key()`.  
Exit/Cancel/Flatten: documented for R5 only — not implemented.

## Risk (`tyrex_pm.risk`)

`RiskContext` (immutable) · `RiskEngine.evaluate` · typed `RiskReason` · ordered policies.  
Duplicate guard owned solely by risk (`IntentDedupRegistry`).

## Planning (`tyrex_pm.planning`)

`ExecutionPlanner.plan` → `PLANNED` | `UNPLANNABLE`. Never submits.

## Runtime config

`ObserveConfig.risk: RiskPlanConfig | None` — when absent, R3 observe-only path.  
When present: mode, notionals, price/spread/liquidity bounds, no-entry window, dedup lifetime, kill switch.

## Forbidden in R4

OMS · orders · fills · portfolio accounting · private Polymarket endpoints · Z-Gap · `old/` · NautilusTrader.
