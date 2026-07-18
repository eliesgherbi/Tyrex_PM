# Reporting and operations

**Purpose:** facts, reports, durable state, reconciliation surfaces.

## Packages / paths

- `tyrex_pm.reporting` — facts JSONL writers
- `tyrex_pm.runtime` — readiness, R7 CLI flows, ack regenerate
- `tyrex_pm.application.cli` — operator commands
- `var/reporting/**` — disposable reports/facts
- `var/state/**` — required operational state
- `config/r7/acknowledgment_policy.json` — sealed identities

## Facts and reports

| Artifact | Role |
|----------|------|
| `facts_<run_id>.jsonl` | Append-only event evidence |
| `report_<run_id>.json` | Structured run summary (+ hashes) |
| Recon JSON under `var/reporting/` | Read-only investigations |

Reports are **not** the policy source of truth.

## Persistent state

| Path | Required? |
|------|-----------|
| `config/r7/acknowledgment_policy.json` | Yes (committed seal) |
| `var/state/r7/position_acknowledgment.json` | Yes for R7 gates |
| `var/state/r7/lifecycle_residuals.json` | Yes when residuals exist |
| Shadow snapshots under `var/state/` | When shadow persistence enabled |

## Reconciliation CLI / scripts

| Tool | Mutations |
|------|-----------|
| `tyrex-pm r7c-recon` | None |
| `tyrex-pm r7-ack-regenerate` | None (rewrites durable ack from sealed policy + inventory) |
| `tyrex-pm live-preflight` | None |
| `scripts/r8_readonly_recon.py` | None |
| `scripts/r7f_exit_rehearsal.py` | None |

## Incident evidence to collect

- report + facts + residuals JSON + stdout
- commit / worktree cleanliness
- never logs of secrets or full wallet addresses

## Limits

- No automatic on-chain cleanup
- Data API positions can disagree with conditional balances — balance wins for execution safety
