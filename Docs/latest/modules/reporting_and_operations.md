# Reporting and operations

**Purpose:** unified run evidence, durable runtime state, reconciliation surfaces.

## Packages / paths

- `tyrex_pm.reporting` — common `RunReporter` / `ReportingPort` (two lanes: audit + analytics)
- `tyrex_pm.runtime` — readiness, R7 CLI flows, N7 one-shot/preflight, ack regenerate
- `tyrex_pm.application.cli` — `--reporting` defaults to `config/reporting/full.yaml`
- `var/runs/<strategy_id>/<run_id>/` — primary run evidence (`manifest.json`, `run_summary.json`, `audit_events.jsonl`, `analytics_events.jsonl`)
- `var/runs/_ops/<tool>/<run_id>/` — operational tool evidence
- `var/runtime_state/**` — local persistent operational state (gitignored; replaces `var/state/`)
- `var/recordings/**` — raw N1/N3 captures (not default trading reports)
- `config/r7/acknowledgment_policy.json` — sealed identities (committed)
- `config/reporting/{full,minimal}.yaml` — optional analytics profiles (mandatory audit cannot be disabled)
- `config/n7_tiny_live.json` — N7 sealed Scope A config

Historical `var/reporting/**` and `var/state/**` trees may still exist as evidence; they are not active write roots.

## Operation effects (taxonomy)

| Effect | Meaning |
|--------|---------|
| Offline / no network | No network I/O |
| Network read | Public or authenticated GET/stream; no venue mutation |
| Local state write | Writes `var/runtime_state` or regenerates ack artifact |
| Report write | Writes under `var/runs` (or `_ops`) |
| Venue mutation | Submit/cancel/heartbeat |
| On-chain mutation | Approval, redeem, transfer, merge/split |

When docs say “no mutations,” specify **which** kind.

## Tool effects

| Tool | Network | Local state | Reports | Venue / on-chain |
|------|---------|-------------|---------|------------------|
| `observe` (fixture) | no | no | yes (`var/runs/...`) | no |
| `observe` / `shadow` (live) | public read | shadow may write snapshot | yes | no venue orders |
| `live-preflight` | public ± L2 read | no (report out) | yes (`_ops`) | no |
| `r7c-recon` | network read | no | yes (`_ops`) | no |
| `r7-ack-regenerate` | network read | **yes** (ack artifact) | optional | no |
| `r7b-live-once --dry-run` | may network-read | no venue write | yes | no venue mutation |
| `r7b-live-once --execute-live` | yes | may update residuals | yes | **venue mutation** |
| `run --mode live --fake-rehearsal` | no (fake) | no | yes | no |
| `run --mode live --live` / `n7-live --live` | yes | may | yes | **venue mutation** |

## Primary LIVE / N7 artifacts

| Path | Role |
|------|------|
| `var/runs/z_gap/<run_id>/manifest.json` | Run identity, mode, fake-transport flag |
| `var/runs/z_gap/<run_id>/run_summary.json` | Accumulators + terminal status |
| `var/runs/z_gap/<run_id>/audit_events.jsonl` | Critical/audit lane |
| `var/runs/z_gap/<run_id>/analytics_events.jsonl` | Optional analytics |
| `var/runs/z_gap/<run_id>/attachments/operator_outcome.json` | N7 operator outcome attachment |

## Durable runtime state

| Path | Role |
|------|------|
| `var/runtime_state/r7/position_acknowledgment.json` | Regenerable ack artifact |
| `var/runtime_state/r7/lifecycle_residuals.json` | Residual registry |
| Shadow snapshots under `var/runtime_state/shadow/` | When shadow persistence enabled |

See `runtime/r7_paths.migrate_state_to_runtime_state` for the one-time `var/state` → `var/runtime_state` migration (no permanent alias; LIVE refuses on conflict).
