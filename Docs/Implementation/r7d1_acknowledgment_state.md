# R7D.1 — Durable acknowledgment state

## Root cause

Deleting `var/reporting/r7/` removed `r7a2_position_acknowledgment.json`.  
`r7b-live-once` previously skipped the ack gate when the file was absent → `DRY_OK` without acknowledgment.

## Directory contract

| Location | Role |
|----------|------|
| `var/reporting/**` | **Disposable** reports, facts, recon dumps |
| `var/state/r7/position_acknowledgment.json` | **Required** durable acknowledgment |
| `var/state/r7/lifecycle_dust.json` | **Required** known R7B dust provenance (not in ack set) |

Report cleanup must never delete `var/state/`.

## Gate (dry and live)

Missing path / missing file / invalid artifact / incomplete inventory / fingerprint mismatch → **BLOCKED**.  
Never silently skip.

## Regeneration

```bash
tyrex-pm r7-ack-regenerate \
  --output var/state/r7/position_acknowledgment.json \
  --report var/reporting/r7d/ack_regenerate_report.json
```

Read-only. Zero mutations. Not permission to sell/redeem/on-chain.
