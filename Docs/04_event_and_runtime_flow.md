# 04 — Event and runtime flow

**Phase:** R5

## Chain

```text
Market/reference event
  → DirectionalSignal
  → ObserveDecision
  → Enter|Exit|Flatten intent
  → RiskDecision
  → ExecutionPlan
  → SubmitOrderCommand
  → OrderSubmitted / OrderAccepted / fills / cancels
  → Portfolio + TradeLifecycle
  → (optional) exit intent → shadow exit → FLAT
```

All retain `correlation_id`. Recovery facts use a new correlation chain while referencing restored entity IDs.

## Intent vs plan vs order

| Object | Meaning |
|--------|---------|
| ObserveDecision | Would-enter / hold / skip |
| Intent | Strategy economic request |
| RiskDecision | Permission |
| ExecutionPlan | Sized limit instruction |
| SubmitOrderCommand | Immutable OMS input |
| Order / Fill | Authoritative execution state |

## Hosts

| Host | When |
|------|------|
| `ObserveHost` | R3/R4 dry (no OMS) |
| `ShadowHost` | R5 when `shadow.enable_oms` |
| `run_live_shadow` | Public live data + ShadowOMS |

## Restart sequence

```text
Load config → load/validate snapshot → restore orders/fills/portfolio/lifecycle/dedup/strategy
→ start reporting → start adapters → wait for fresh books → resume evaluation
```

No new entry before recovery completes and market data is ready.
