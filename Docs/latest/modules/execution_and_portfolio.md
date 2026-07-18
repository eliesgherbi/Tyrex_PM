# Execution and portfolio

**Purpose:** OMS boundary, settlement, fills, positions, residuals.

## Packages

- `tyrex_pm.execution.protocol` — `OMS`
- `tyrex_pm.execution.shadow_oms`
- `tyrex_pm.execution.polymarket` — transport, auth, LiveOMS, settlement, reconcile, residuals hooks
- `tyrex_pm.execution.order_store` / `fill_ledger`
- `tyrex_pm.portfolio` / `tyrex_pm.lifecycle`
- `tyrex_pm.runtime.r7_lifecycle_residuals` / ack modules

## OMS protocol

```python
class OMS(Protocol):
    def submit(self, command: SubmitOrderCommand) -> OrderId: ...
    def cancel(self, command: CancelOrderCommand) -> None: ...
    def stop(self) -> None: ...
```

`submit` returns **local** `OrderId` only — never implies venue fill.

| Implementation | Mutations |
|----------------|-----------|
| `ShadowOMS` | Local paper only |
| `LiveOMS` + mutation transport | Real CLOB when armed |

## Signer vs funder

- Signer EOA signs / L2 `POLY_ADDRESS`
- Funder/proxy owns balances when `signature_type` is proxy/safe/1271
- Refuse signer-as-conditional-owner mistakes (address roles)

## Settlement and inventory

- Insert `matched` ≠ trade `CONFIRMED`
- Inventory from **CONFIRMED** trades + funder conditional balance
- Partial fills / FAK no-match → bounded replan; then manual intervention if exhausted

## Portfolio and residuals

- Portfolio applies fills to positions
- Residual registry records non-tradable dust / incomplete exits
- Cleanup policy **`NONE`** — no auto redeem/merge/transfer

## Invariants

- Unknown submission → reconcile first; do not resubmit blindly
- Unknown external orders never auto-canceled
- Dust ≠ exact flat

## Tests / evidence

- R6–R8 execution suites; live evidence under [`../../implementation/`](../../implementation/)

## Limits

- Generic continuous live not productized
- Heartbeat endpoint can cancel opens if misused — not armed in safe paths
