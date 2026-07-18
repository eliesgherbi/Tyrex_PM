# Strategy, risk, and planning

**Purpose:** from signals to authorized, sized execution plans.

## Packages

- `tyrex_pm.strategies` (+ `framework_validation.reference_momentum`)
- `tyrex_pm.core.intents`
- `tyrex_pm.risk`
- `tyrex_pm.planning`
- `tyrex_pm.execution.polymarket.lifecycle_exit_plan` (exit pricing)
- `tyrex_pm.runtime.r7_lifecycle_policy` (numeric policy constants)

## Strategy interface

`Strategy` protocol: `on_start` / `on_signal` / `on_stop`.  
`on_signal` returns `(ObserveDecision, list[EnterIntent])` (exits via related intent types in validation strategy).

**ReferenceMomentumStrategy:** structural validation only — not a recommended trading strategy.

## Risk

- `RiskEngine.evaluate(intent, RiskContext) → RiskDecision`
- Dedup registry prevents intent storms
- Generic `LIVE_TINY` remains denied outside the guarded R7 one-shot path
- Kill switch and spread/liquidity/price bounds from config

## Planning

| Planner | Owns |
|---------|------|
| Entry sizing | BUY limit, qty, fee-inclusive collateral bound |
| Exit planner | Bid-side FAK limit, depth, freshness, floors |

### Fee bound vs actual fee

- Entry uses an **estimated max fee** to keep fee-inclusive collateral ≤ cap.
- Venue trade records may show `fee_rate_bps=0` or omit fee amount.
- Bounds are **not** realized P&L fees.

## Invariants

- Strategy must not import live Polymarket execution package
- Never reuse entry BUY limit as SELL limit
- SELL qty ≤ `min(confirmed_acquired, sellable_balance, remaining_after_confirmed_exits)`

## Tests

- `tests/test_r7e_exit_plan.py`, `test_r7f_exit_integration.py`, `test_r8_flat_with_dust_terminal.py`, risk/strategy unit tests

## Limits

- Z-Gap contracts not present
- Emergency vs normal differ mainly by touch-slippage cap (see exit-floor policy)
