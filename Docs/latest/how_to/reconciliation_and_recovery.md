# Reconciliation and recovery

**Purpose:** what to check when restarting, investigating residuals, or stopping safely.

## Effects of recon tools

| Tool | Network read | Local state write | Report write | Venue / on-chain |
|------|--------------|-------------------|--------------|------------------|
| `r7c-recon` | yes | no | yes | no |
| `r7-ack-regenerate` | yes | **yes** (ack artifact) | optional | no |
| `scripts/r8_readonly_recon.py` | yes | no | yes | no |
| `scripts/r7f_exit_rehearsal.py` | yes | no | yes | no |

“No mutations” means **no venue/on-chain mutation**. Ack regenerate still writes local state.

## Startup checks

1. Working directory = repo root; expected commit known  
2. For venue-mutation paths: `git status` clean  
3. Ack artifact present or regenerable from sealed policy  
4. Residual registry loadable; no unexpected tradable residual  
5. Credentials roles correct (signer ≠ funder when proxy)  
6. Market/books/reference freshness before ready  

## Open orders

- Expect zero open orders on selected token after a completed lifecycle  
- Unknown external orders: surface and stop — never auto-cancel  

## Positions and balances

- Data API positions: informational; may include sealed acknowledged resolved positions  
- Conditional balances: authoritative for dust / sellability  
- Inventory states: `FLAT` \| `FLAT_WITH_DUST` \| `RESIDUAL_EXPOSURE` \| `UNKNOWN`  

## Lifecycle outcome vs inventory state

Keep separate (see [state_lifecycle_recovery](../concepts/state_lifecycle_recovery.md)):

- Inventory: balance class only  
- Outcome/provenance: automatic completion, external/manual flatten (`FLAT_EXTERNAL_ACTION` in some enums), manual intervention, blocked, dry-ok  

Example: external flatten with leftover dust → outcome external/manual · inventory `FLAT_WITH_DUST`.

## Settlement states

`MATCHED` / `MINED` are not inventory. Wait for `CONFIRMED` + sellable balance before SELL logic.

## Manual UI action

If runtime stops at manual intervention / tradable residual:

1. Do not re-fire live blindly  
2. Inspect report/facts/residuals  
3. Flatten in UI only if sellable and operator-approved  
4. Reconcile with `r7c-recon` / readonly scripts  
5. Classify inventory from balance; record provenance separately  

## Unexpected exposure

- Extra resolved positions are **not** auto-acknowledged  
- Unexpected tradable size blocks entry  
- Keep visible in account-wide recon  

## Missing acknowledgment

- Fail closed  
- Regenerate via `tyrex-pm r7-ack-regenerate` from sealed policy + inventory (network read + local state write)  
- Do not invent identities from every resolved position  

## Residual dust

- Cleanup policy `NONE` — no automatic redemption  
- Record counts are snapshot evidence (R8), not evergreen invariants  

## When to stop

Stop automated action if: unknown submission, tradable residual after attempt cap, ack gate fail, dirty worktree on live, credential role mismatch, or unexplained tradable exposure.

## Incident checklist

- [ ] Capture stdout  
- [ ] Preserve `report_*.json` + `facts_*.jsonl`  
- [ ] Preserve `var/state/r7/lifecycle_residuals.json` + ack artifact  
- [ ] Note commit SHA + worktree cleanliness  
- [ ] Redact secrets/addresses  
- [ ] Separate inventory state from lifecycle outcome  
- [ ] Do not start a new live phase without explicit scope  

R8 snapshot evidence: [`../../implementation/r8_framework_acceptance.md`](../../implementation/r8_framework_acceptance.md).
