# Reconciliation and recovery

**Purpose:** what to check when restarting, investigating residuals, or stopping safely.

## Startup checks

1. Working directory = repo root; expected commit known
2. For mutation-capable paths: `git status` clean
3. Durable ack present (`var/state/r7/position_acknowledgment.json`) matching sealed policy
4. Residual registry loadable; no unexpected tradable residual
5. Credentials roles correct (signer ≠ funder when proxy)
6. Market/books/reference freshness before ready

## Open orders

- Expect **zero** open orders on selected token after a completed lifecycle
- Unknown external orders: surface and stop — never auto-cancel

## Positions and balances

- Data API positions: sealed four ack identities remain visible
- Conditional balances: authoritative for dust / sellability
- Exact zero → `FLAT`; dust `< 0.01` → `FLAT_WITH_DUST`

## Settlement states

`MATCHED` / `MINED` are not inventory. Wait for `CONFIRMED` + sellable balance before SELL logic.

## Manual UI action

If runtime stops at `MANUAL_INTERVENTION` / `RESIDUAL_EXPOSURE`:

1. Do not re-fire live blindly
2. Inspect report/facts/residuals
3. Flatten in UI only if sellable and operator-approved
4. Reconcile with `r7c-recon` / readonly scripts
5. Expect possible `FLAT_EXTERNAL_ACTION`

## Unexpected exposure

- Fifth resolved position is **not** auto-acknowledged
- Unexpected tradable size blocks entry
- Keep it visible in account-wide recon

## Missing acknowledgment

- Fail closed
- Regenerate only via `tyrex-pm r7-ack-regenerate` from sealed policy + inventory
- Do not invent identities from every resolved position

## Residual dust

- Three lifecycle dust records may exist post-R8
- Cleanup policy `NONE` — no automatic redemption

## When to stop

Stop automated action if: unknown submission, tradable residual after attempt cap, ack gate fail, dirty worktree on live, credential role mismatch, or unexplained fifth exposure.

## Incident checklist

- [ ] Capture stdout
- [ ] Preserve `report_*.json` + `facts_*.jsonl`
- [ ] Preserve `var/state/r7/lifecycle_residuals.json` + ack artifact
- [ ] Note commit SHA + worktree cleanliness
- [ ] Redact secrets/addresses before sharing
- [ ] Classify terminal (`FLAT` vs `FLAT_WITH_DUST` vs residual)
- [ ] Do not start a new live phase without explicit scope

Evidence index: [`../../implementation/r8_framework_acceptance.md`](../../implementation/r8_framework_acceptance.md).
