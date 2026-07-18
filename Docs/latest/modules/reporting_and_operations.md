# Reporting and operations

**Purpose:** facts, reports, local persistent state, reconciliation surfaces.

## Packages / paths

- `tyrex_pm.reporting` — facts JSONL writers
- `tyrex_pm.runtime` — readiness, R7 CLI flows, ack regenerate
- `tyrex_pm.application.cli`
- `var/reporting/**` — runtime-disposable evidence
- `var/state/**` — local persistent operational state (gitignored)
- `config/r7/acknowledgment_policy.json` — sealed identities (committed)

## Operation effects (taxonomy)

| Effect | Meaning |
|--------|---------|
| Offline / no network | No network I/O |
| Network read | Public or authenticated GET/stream; no venue mutation |
| Local state write | Writes `var/state` or regenerates ack artifact |
| Report write | Writes under `var/reporting` |
| Venue mutation | Submit/cancel/heartbeat |
| On-chain mutation | Approval, redeem, transfer, merge/split |

When docs say “no mutations,” specify **which** kind.

## Tool effects

| Tool | Network | Local state | Reports | Venue / on-chain |
|------|---------|-------------|---------|------------------|
| `observe` (fixture) | no | no | yes (facts) | no |
| `observe` / `shadow` (live) | public read | shadow may write snapshot | yes | no venue orders |
| `live-preflight` | public ± L2 read | no (report out) | yes | no |
| `r7c-recon` | network read | no | yes | no |
| `r7-ack-regenerate` | network read | **yes** (ack artifact) | optional | no |
| `scripts/r8_readonly_recon.py` | network read | no | yes | no |
| `scripts/r7f_exit_rehearsal.py` | public read | no | yes | no |
| `r7b-live-once --dry-run` | may network-read | no venue write | yes | no venue mutation |
| `r7b-live-once --execute-live` | yes | may update residuals | yes | **venue mutation** |

## Facts and reports

| Artifact | Role |
|----------|------|
| `facts_<run_id>.jsonl` | Append-only event evidence |
| `report_<run_id>.json` | Structured run summary |
| Recon JSON under `var/reporting/` | Investigations |

Reports are not the policy source. Deleting them destroys evidence but not sealed policy.

## Local persistent state

| Path | Role |
|------|------|
| `config/r7/acknowledgment_policy.json` | Sealed identities (committed) |
| `var/state/r7/position_acknowledgment.json` | Regenerable ack artifact |
| `var/state/r7/lifecycle_residuals.json` | Residual registry |
| Shadow snapshots under `var/state/` | When shadow persistence enabled |

Not database-grade durability.

## Incident evidence to collect

- stdout, report, facts, residuals JSON, commit SHA, worktree cleanliness  
- Redact secrets and full addresses  

## Limits

- No automatic on-chain cleanup  
- Conditional balance wins over Data API for execution safety  
