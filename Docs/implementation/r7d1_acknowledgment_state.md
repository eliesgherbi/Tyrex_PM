# R7D.1 — Durable acknowledgment state

**Superseded in part by R7D.2** (`Docs/implementation/r7d2_operator_handoff.md`):
sealed acknowledgment policy + multi-record residual registry.

## Root cause

Deleting `var/reporting/r7/` removed `r7a2_position_acknowledgment.json`.  
`r7b-live-once` previously skipped the ack gate when the file was absent → `DRY_OK` without acknowledgment.

## Directory contract

| Location | Role |
|----------|------|
| `var/reporting/**` | **Disposable** reports, facts, recon dumps |
| `config/r7/acknowledgment_policy.json` | **Sealed** exact four identities (R7D.2) |
| `var/state/r7/position_acknowledgment.json` | **Required** durable acknowledgment |
| `var/state/r7/lifecycle_residuals.json` | **Required** multi-record residuals (R7D.2) |
| `var/state/r7/lifecycle_dust.json` | Legacy scalar; migrated into residuals |

Report cleanup must never delete `var/state/` or `config/r7/`.

## Gate (dry and live)

Missing path / missing file / invalid artifact / incomplete inventory / fingerprint mismatch → **BLOCKED**.  
Never silently skip.

## Regeneration

```bash
tyrex-pm r7-ack-regenerate \
  --output var/state/r7/position_acknowledgment.json \
  --report var/reporting/r7d/ack_regenerate_report.json
```

Read-only. Zero mutations. Uses sealed policy identities only — not every resolved position. Not permission to sell/redeem/on-chain.
