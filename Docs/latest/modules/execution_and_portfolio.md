# Execution and portfolio

**Purpose:** OMS boundary, settlement, fills, positions, residuals.

## Packages

- `tyrex_pm.execution.protocol` — `OMS`
- `tyrex_pm.execution.shadow_oms`
- `tyrex_pm.execution.polymarket` — transport, auth, LiveOMS, settlement, reconcile
- `tyrex_pm.execution.order_store` / `fill_ledger`
- `tyrex_pm.portfolio` / `tyrex_pm.lifecycle`
- R7 residuals/ack under `tyrex_pm.runtime.r7_*` (**phase-specific**)

## OMS protocol (active)

```python
class OMS(Protocol):
    def submit(self, command: SubmitOrderCommand) -> OrderId: ...
    def cancel(self, command: CancelOrderCommand) -> None: ...
    def stop(self) -> None: ...
```

`submit` returns **local** `OrderId` only.

| Implementation | Venue mutation |
|----------------|----------------|
| `ShadowOMS` | None (paper) |
| `LiveOMS` + mutation transport | Real CLOB when armed |

## Authority hierarchy

| Layer | Authority |
|-------|-----------|
| Internal accounting | `Portfolio` from confirmed execution events |
| Orders / fills | `OrderStore` / `FillLedger` |
| External evidence | Authenticated trades + funder conditional balance |
| Disagreement | `UNKNOWN` / block / manual intervention |

Portfolio is authoritative for **derived internal** positions; it cannot overrule venue evidence. Conditional balance establishes sellability. Data API may lag.

## Signer vs funder

- Signer EOA → L2 header `POLY_ADDRESS` (derived from private key)
- Env `POLYMARKET_ADDRESS` is an optional **signer** override for tests — never a funder alias
- Funder/proxy from `TYREX_FUNDER` / `POLYMARKET_FUNDER` when proxy modes
- Confusing signer and funder previously caused authenticated L2 failures (R6D)

## Settlement and inventory

- Insert `matched` ≠ trade `CONFIRMED`
- Inventory from **CONFIRMED** trades + funder conditional balance
- Inventory states: `FLAT` \| `FLAT_WITH_DUST` \| `RESIDUAL_EXPOSURE` \| `UNKNOWN`
- `FLAT_EXTERNAL_ACTION` is a **lifecycle outcome/provenance**, not an inventory state (still present on some runtime enums — debt)

## Fee / P&L note

Use the vocabulary in [strategy_risk_planning](strategy_risk_planning.md). Fee bounds ≠ confirmed fees.

## Residuals

- Residual registry records dust / incomplete exits
- Cleanup policy **`NONE`** — no auto redeem/merge/transfer
- Counts of residual records are snapshot evidence (see R8 report), not timeless invariants

## Invariants

- Unknown submission → reconcile first; do not resubmit blindly
- Unknown external orders never auto-canceled
- Dust ≠ exact flat

## Limits

- Generic continuous live not productized
- Heartbeat unsupported in safe paths (can cancel opens if misused)
