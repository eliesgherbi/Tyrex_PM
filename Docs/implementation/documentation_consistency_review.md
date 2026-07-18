# Documentation consistency review

**Date:** 2026-07-18  
**Docs baseline:** `0c0942e`  
**Framework baseline:** `fb9d0d8`  
**Branch:** `rest_project`  
**Scope:** documentation + docs tests only (no runtime, no live, no `.env` / `var/state` edits)

## Claim-to-code mismatches found and corrected

| Document / claim | Evidence | Correction |
|------------------|----------|------------|
| “Dependency flow” diagram was runtime sequence | Import audit of `core`/`strategies`/`risk`/`planning`/`adapters` | Split **runtime flow** vs **static dependency rules** in `architecture.md` + `modules/overview.md` |
| Portfolio “owns position truth” vs venue balance authority | `settlement.classify_flatness`, LiveOMS recon | Layered authority table (internal vs venue) |
| `FLAT_EXTERNAL_ACTION` listed as inventory class | `FlatClassification` has no such member; it exists on `TerminalOutcome` / `MutationPhase` / `SettlementPhase` | Two-axis model; inventory = FLAT / FLAT_WITH_DUST / RESIDUAL_EXPOSURE / UNKNOWN |
| Tools labeled “read-only” while writing disk | `r7-ack-regenerate`, report scripts | Operation-effects taxonomy |
| “Durable state” language | `var/` gitignored; no DB durability SLA | “Local persistent state” vs “runtime-disposable evidence” |
| Four ack / three dust as evergreen invariants | R8 recon snapshot only | Moved to labeled R8 snapshot + evidence links |
| Env: unclear `POLY_ADDRESS` vs `POLYMARKET_ADDRESS` | `auth.load_l2_credentials` | Header vs env override; alias precedence; signer≠funder warning |
| Protocol implied ExitIntent returns / timers | `strategies/protocol.py` | Documented actual callbacks; protocol vs `ReferenceMomentum` return-type debt |
| `discover-btc-window` without `--which` | CLI help | Documented `--which` |
| Dry = offline | `r7b-live-once` dry path | Dry = no venue mutation; may network-read + report-write |
| Fee/P&L vocabulary vague | Live reports / fee sizing | Explicit bound vs estimated vs confirmed terms |

## Runtime flow versus dependency flow

- Runtime: venue payload → … → facts (documented as control/data flow).  
- Static: core̸→adapters; strategies̸→polymarket execution; risk/planning̸→strategies; composition roots may wire concretes.  
- Debt: protocol imports `ObserveDecision` from validation strategy; `on_signal` typed narrower than `ReferenceMomentumStrategy`.

## Authority hierarchy

Internal accounting (`Portfolio`/`OrderStore`/`FillLedger`) vs external venue evidence (trades + funder conditional balance) vs reconciliation on disagreement → UNKNOWN / block / manual intervention.

## Lifecycle outcome versus inventory state

| Axis | Allowed values (docs) | Code note |
|------|------------------------|-----------|
| Inventory | FLAT, FLAT_WITH_DUST, RESIDUAL_EXPOSURE, UNKNOWN | `FlatClassification` (+ `ACTIVE` synonym) |
| Outcome / provenance | automatic complete, external/manual flatten, manual intervention, blocked, dry-ok | Still mixed into `TerminalOutcome` / phases — **debt** |

## Operation-effects taxonomy

Offline · Network read · Local state write · Report write · Venue mutation · On-chain mutation — applied to quickstart, run_modes, configuration, reporting, recon, polymarket.

## Persistence terminology

`var/state/` = local persistent operational state (gitignored).  
`var/reporting/` = runtime-disposable evidence (still important for audit).  
Not database-grade durability.

## Mutable snapshot cleanup

Counts of ack positions / dust records removed from evergreen concept language; retained as **R8 acceptance snapshot at `fb9d0d8`** with link to `r8_framework_acceptance.md`.

## Environment / config corrections

Preferred: `TYREX_PRIVATE_KEY`, `TYREX_FUNDER`, `TYREX_SIGNATURE_TYPE`.  
Deprecated aliases: `POLYMARKET_PK`, `POLYMARKET_FUNDER`, `POLYMARKET_SIGNATURE_TYPE`.  
Passphrase: `POLYMARKET_PASSPHRASE` or `POLYMARKET_API_PASSPHRASE`.  
`POLYMARKET_ADDRESS`: signer override for tests.  
`POLY_ADDRESS`: L2 **header** = signer.

## Interface / callback corrections

Active: `on_start` / `on_signal` / `on_stop`.  
Absent: `on_timer`, `on_execution_event`.  
OMS: `submit` / `cancel` / `stop`.  
Intents exist: Enter/Exit/Cancel/Flatten; protocol return list is Enter-only (debt).

## Phase-specific debt labeling

R7 one-shot, `config/r7`, `var/state/r7`, ack/residual CLI, guarded ReferenceMomentum composition marked phase-specific; Z-Gap must not import `runtime/r7*`.

## Fee / P&L clarification

Defined BUY notional, SELL proceeds, gross price P&L, max fee bound, estimated fee, confirmed actual fee, realized net vs unknown net — fee bound ≠ confirmed expense.

## Tests and results

- Added `tests/test_docs_consistency.py`  
- Kept `tests/test_docs_links.py`  
- Full suite: **384 passed**

## Files changed

- Multiple `Docs/latest/**` pages (concepts, modules, how_to, integrations, developer_guide, READMEs)  
- `Docs/README.md` (local-persistent wording)  
- `Docs/implementation/documentation_consistency_review.md` (this file)  
- `tests/test_docs_consistency.py`  
- **No `src/` changes**

## Remaining uncertainties

- Exact dry-run network surface area varies with injected transports vs live SDK wiring — documented as “may network-read.”  
- `SettlementPhase.FLAT_EXTERNAL_ACTION` still blurs axes until a future typed split.  
- `FlatClassification.ACTIVE` synonym retained in code; docs treat closed-lifecycle inventory as four terminals.  
- Optional external markdownlint still not configured.

## Verdict path

Docs now state current code truth with explicit debt labels rather than overstating a clean two-axis type system.
