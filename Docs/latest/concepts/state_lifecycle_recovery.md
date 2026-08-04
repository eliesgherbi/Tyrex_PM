# State, lifecycle, and recovery

**Purpose:** who owns truth, how inventory is classified, and what local persistence means.

## Authoritative owners (internal)

| Concern | Owner | Path |
|---------|-------|------|
| Instruments | Domain / discovery mapping | `domain/polymarket` |
| Books | `BookStore` | `market_data/book_store.py` |
| Reference prices | Reference store | market_data / adapters |
| Orders | `OrderStore` | `execution/order_store.py` |
| Fills | `FillLedger` | `execution/fill_ledger.py` |
| Derived positions | `Portfolio` | `portfolio/portfolio.py` |
| Host trade phase (shadow path) | `TradeLifecycle` | `lifecycle/trade_lifecycle.py` |
| Sealed ack identities | Acknowledgment policy | `config/r7/acknowledgment_policy.json` |
| Regenerated ack artifact | Local ack file | `var/runtime_state/r7/position_acknowledgment.json` |
| Lifecycle residuals | Residual registry | `var/runtime_state/r7/lifecycle_residuals.json` |

Adapters and strategies do **not** own order or portfolio truth.

## Authority hierarchy (internal vs venue)

| Layer | Authority |
|-------|-----------|
| Internal runtime accounting | `Portfolio` from confirmed execution events |
| Order / fill state | `OrderStore` / `FillLedger` |
| External venue evidence | Authenticated trades + funder conditional balance |
| Reconciliation | Compares internal vs external |
| Disagreement | `UNKNOWN`, block readiness, or manual intervention |

Portfolio cannot overrule venue evidence. Conditional balance establishes **sellability**. Data API positions may lag. Missing evidence never means flat.

## Two axes: lifecycle outcome vs inventory state

These concepts are **independent**. Document them separately even though some runtime enums still mix them (technical debt).

### Inventory state (balance classification)

From `FlatClassification` in `settlement.py` (authoritative balance classes):

| Inventory state | Meaning |
|-----------------|---------|
| `FLAT` | Conditional balance exactly zero |
| `FLAT_WITH_DUST` | `0 < balance < min_tradable` (default `0.01`) |
| `RESIDUAL_EXPOSURE` | Tradable leftover (`≥ min_tradable`) |
| `UNKNOWN` | Reliable balance unavailable |

(`FlatClassification.ACTIVE` exists as a synonym for open tradable residual in some paths — not a fourth terminal inventory class for closed lifecycles.)

Do **not** treat `FLAT_EXTERNAL_ACTION` as an inventory state.

### Lifecycle outcome / provenance

How the process ended (examples — not a single clean enum today):

| Outcome idea | Runtime evidence today |
|--------------|------------------------|
| Automatic lifecycle completed | `TerminalOutcome.FLAT` or `FLAT_WITH_DUST` with `lifecycle_completed` |
| Externally / manually flattened | `TerminalOutcome.FLAT_EXTERNAL_ACTION` / `MutationPhase.FLAT_EXTERNAL_ACTION` |
| Manual intervention required | `TerminalOutcome.MANUAL_INTERVENTION` |
| Blocked before mutation | `TerminalOutcome.BLOCKED` |
| Dry validation ok | `TerminalOutcome.DRY_OK` |

**Examples**

- Lifecycle outcome: external/manual flatten · Inventory state: `FLAT_WITH_DUST`
- Lifecycle outcome: automatic completion · Inventory state: `FLAT`

### Technical debt

`TerminalOutcome`, `SettlementPhase`, and `MutationPhase` still embed values such as `FLAT_EXTERNAL_ACTION` alongside inventory-like names. Prefer the two-axis model in docs and new code; do not pretend a fully typed split already exists.

Shadow host `TradeLifecycle` states (`FLAT`, `ENTRY_PENDING`, `ACTIVE`, …) are a separate host phase machine — not the same as venue inventory classification.

## Local persistent state vs runtime-disposable evidence

```text
var/runtime_state/  local persistent operational state (gitignored; replaces var/state/)
var/runs/           runtime-disposable evidence (reports, audit events JSONL)
```

Historical `var/state/` and `var/reporting/` trees may still exist on disk as evidence
from before the `var/runtime_state` / `var/runs` migration; they are not active write
roots. See [reporting_and_operations](../modules/reporting_and_operations.md) for the
one-time migration helper.

This is **not** database-grade durability (no documented atomicity/backup/recovery SLA).

| Fact | Implication |
|------|-------------|
| `var/runtime_state` is gitignored | Local machine memory, not git history |
| Deleting `var/runtime_state` | Removes required operational memory for gates |
| Acknowledgment artifact | Regenerable from sealed `config/r7/acknowledgment_policy.json` + inventory |
| Residual records | May need venue recon + historical facts to reconstruct safely |
| Deleting reports | Does not change policy; destroys audit/incident evidence |

Prefer “local persistent state” over “durable state” unless recovery guarantees are explicitly documented.

## Restart and reconciliation

1. Load config + credentials (signer ≠ funder when proxy).
2. Restore local snapshots where applicable (e.g. shadow).
3. Reconcile open orders / positions (never auto-cancel unknown externals).
4. Require fresh books/reference before ready.
5. For R7 one-shot: ack gate + residual gate.

## R8 acceptance snapshot (not a timeless invariant)

At commit `fb9d0d8`, acceptance recon observed four sealed acknowledged resolved positions and three lifecycle dust records.  
See [`../../implementation/r8_framework_acceptance.md`](../../implementation/r8_framework_acceptance.md).  
Mechanisms (sealed policy, residual registry, cleanup `NONE`) are generic; counts are snapshot evidence only.
