# 04 — Event and runtime flow

**Phase:** R6A

## Chain

```text
Market/reference event
  → signal → observe decision
  → retry-gated intent
  → risk → plan → command
  → OMS.submit (local OrderId)
  → OrderSubmitted / Accepted|Rejected|Unknown
  → fills → portfolio → lifecycle
  → (live) reconcile → readiness
```

## Hosts / runners

| Component | Role |
|-----------|------|
| `ObserveHost` / `TradingHost` | Single evaluate pipeline |
| `ShadowHost` | OMS + lifecycle + retry hooks |
| `live_runner.run_live` | Shared live adapter loop |
| `LiveOMS` | Venue adapter; mutations off in R6 |

## Restart / live readiness

```text
Config → credentials → market → persistence → transport
→ user stream → reconcile → books/reference fresh → ready
```

Public market-data health cannot override private execution unreadiness.
