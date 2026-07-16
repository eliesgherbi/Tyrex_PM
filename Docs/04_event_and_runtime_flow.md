# 04 — Event and runtime flow

**Phase:** R4

## Chain

```text
ReferencePriceUpdated
  → DirectionalSignal
  → ObserveDecision          (R3)
  → EnterIntent              (R4 transition)
  → RiskDecision             (R4)
  → ExecutionPlan | UNPLANNABLE   (R4 dry)
```

All retain `correlation_id`. Facts record compact IDs + reason codes.

## Intent vs plan vs order

| Object | Meaning in R4 |
|--------|----------------|
| ObserveDecision | Would-enter / hold / skip (no economic request) |
| EnterIntent | Strategy request for notional |
| RiskDecision | Permission |
| ExecutionPlan | Dry future OMS input |
| Order | **Not created** |

## Modes

`OBSERVE` / `SHADOW` evaluate the chain without submission.  
`LIVE_TINY` → `LIVE_NOT_SUPPORTED` before any plan.

## One host

`ObserveHost` (+ `run_live_observe`) remains the only composition root.
