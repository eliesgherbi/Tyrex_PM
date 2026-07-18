# State, lifecycle, and recovery

**Purpose:** who owns truth, how flatness is classified, and what survives restart.

## Authoritative owners

| Concern | Owner | Package / path |
|---------|-------|----------------|
| Instruments | Instrument registry / domain mapping | `domain/polymarket`, discovery |
| Books | `BookStore` | `market_data/book_store.py` |
| Reference prices | Reference store | market_data / adapters |
| Orders | `OrderStore` | `execution/order_store.py` |
| Fills | `FillLedger` | `execution/fill_ledger.py` |
| Positions | `Portfolio` | `portfolio/portfolio.py` |
| Trade phase | `TradeLifecycle` | `lifecycle/trade_lifecycle.py` |
| Sealed ack identities | Acknowledgment policy | `config/r7/acknowledgment_policy.json` |
| Regenerated ack artifact | Durable ack file | `var/state/r7/position_acknowledgment.json` |
| Lifecycle residuals | Residual registry | `var/state/r7/lifecycle_residuals.json` |

Adapters and strategies do **not** own portfolio or order truth.

## Inventory terminal classes

| Classification | Meaning |
|----------------|---------|
| `FLAT` | Conditional balance **exactly** zero |
| `FLAT_WITH_DUST` | `0 < balance < min_tradable` (currently `0.01`) — not tradable exposure |
| `RESIDUAL_EXPOSURE` | Tradable leftover (`≥ min_tradable`) |
| `UNKNOWN` | Balance/evidence incomplete |
| `FLAT_EXTERNAL_ACTION` | Flatten observed via external/manual action path |

Do **not** call dust `FLAT`. Lifecycle may be “completed successfully” while inventory terminal is `FLAT_WITH_DUST`.

## Durable state vs disposable reports

```text
var/state/        required operational state (ack, residuals, snapshots)
var/reporting/    disposable evidence (reports, facts JSONL)
```

Deleting `var/reporting/**` must not remove safety policy. Deleting durable ack without regeneration blocks entry (fail-closed).

## Restart and reconciliation

On live/readiness paths:

1. Load config + credentials (roles: signer ≠ funder when proxy).
2. Restore persistence snapshots where applicable (e.g. shadow).
3. Reconcile open orders / positions (unknown external orders are never auto-canceled).
4. Require fresh books/reference before ready.
5. Ack gate + residual gate for R7 one-shot.

Missing evidence never means flat.

## Current account invariants (post-R8)

- Exactly **four** sealed acknowledged resolved positions — untouched.
- **Three** distinct lifecycle dust records (`FLAT_WITH_DUST`) — cleanup `NONE`.
- No automatic redemption or on-chain cleanup.

Evidence: [`../../implementation/r8_framework_acceptance.md`](../../implementation/r8_framework_acceptance.md).
