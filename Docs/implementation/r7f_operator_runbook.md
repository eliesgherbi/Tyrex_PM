# R7F operator runbook — **CLOSED**

**R7 live validation is complete.** The successful third controlled lifecycle is run `55fd9a76-743b-4fb8-835d-adcdbf0f517a` (see [`r7_successful_live_acceptance.md`](r7_successful_live_acceptance.md)).

**No further R7 live run is required.**  
**Do not provide or execute another `--execute-live` command under R7.**  
Any future live test requires a **new explicitly scoped phase** (not this runbook).

Agent must never run `--execute-live`.

## Completed third lifecycle (archived)

| Item | Value |
|------|-------|
| Run | `55fd9a76-743b-4fb8-835d-adcdbf0f517a` |
| Commit | `3df3e21` |
| Market | `btc-updown-5m-1784318400` |
| BUY @ 0.51 → SELL @ 0.50 | both CONFIRMED |
| Inventory terminal | `FLAT_WITH_DUST` (`0.000587`) |
| Manual exit | not required |

### Archived live command (historical — do not re-run for R7)

```bash
# HISTORICAL ONLY — R7 third live already completed as 55fd9a76.
# Not authorized for re-execution under R7 closure.
python -m tyrex_pm.application.cli r7b-live-once \
  --strategy reference-momentum \
  --market-family btc_updown_5m \
  --max-windows 3 \
  --max-buy-collateral 5.00 \
  --execute-live \
  --output-dir var/reporting/r7f_third_live
```

## Read-only commands retained

```bash
cd /e/polymarket/Tyrex_PM
python scripts/r8_readonly_recon.py
python scripts/r7e_readonly_recon.py
python scripts/r7f_exit_rehearsal.py
python -m tyrex_pm.application.cli r7c-recon --help
python -m tyrex_pm.application.cli r7-ack-regenerate --help
```

Dry-run (read-only path; zero mutations) remains available for regression:

```bash
python -m tyrex_pm.application.cli r7b-live-once \
  --strategy reference-momentum \
  --market-family btc_updown_5m \
  --max-windows 3 \
  --max-buy-collateral 5.00 \
  --dry-run \
  --output-dir var/reporting/r8_dry
```

## Incident response (no live re-entry)

If an unexpected tradable residual appears:

1. Do **not** re-run live under this runbook.
2. Flatten in UI only if sellable and operator-approved.
3. Preserve report + facts + `var/state/r7/lifecycle_residuals.json`.
4. Open a new scoped phase before any automated mutation.

## Acceptable inventory terminals (reference)

`FLAT` · `FLAT_WITH_DUST` · `FLAT_EXTERNAL_ACTION`

`FLAT` means exact-zero conditional balance — **not** “no tradable exposure.” Dust ⇒ `FLAT_WITH_DUST`.
