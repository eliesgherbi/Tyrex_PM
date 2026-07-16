# 03 — Module contracts

**Phase:** R5

## Strategy

Protocol: `on_start`, `on_signal`/`apply_transition`, `on_stop`.  
Must not import adapters, risk, planner, OMS, or portfolio.

When `DecisionContext.lifecycle` is `None` (R4 dry): signal-transition rules.  
When lifecycle is provided (R5): eligibility uses lifecycle/position; exit precedence applies.

## Intents

| Intent | Role |
|--------|------|
| `EnterIntent` | BUY, `target_notional` |
| `ExitIntent` | Target-flat reduce |
| `FlattenIntent` | Urgent flat |
| `CancelIntent` | Cancel working order |

## OMS (`tyrex_pm.execution.protocol`)

```python
class OMS(Protocol):
    def submit(self, command: SubmitOrderCommand) -> OrderId: ...
    def cancel(self, command: CancelOrderCommand) -> None: ...
    def stop(self) -> None: ...
```

`ShadowOMS` implements this now; live Polymarket adapter in R6.  
`submit` does not return fills — results arrive as execution events.

## Order / fill / portfolio

- Order states: `CREATED → SUBMITTED → ACCEPTED → PARTIALLY_FILLED|FILLED|CANCEL_*|REJECTED`
- Fills are immutable ledger records keyed by `execution_id`
- Portfolio is long-only; oversell fails closed

## Risk

Entry: notional/position/exposure caps, pending-order block, freshness, spread, liquidity.  
Exit/flatten: cannot increase exposure; kill switch denies entries but permits risk-reducing actions.  
Missing portfolio view fails closed (except R4 dry `exposure_available=False`).

## Forbidden in R5

Private Polymarket trading endpoints · real order signing · wallet credential use · `old/` · NautilusTrader · Z-Gap.
