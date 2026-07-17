# R7F operator runbook — third controlled lifecycle

**Agent must never run `--execute-live`.**

## Preflight (read-only)

```bash
cd /e/polymarket/Tyrex_PM
git status --porcelain   # must be empty for live
python scripts/r7f_exit_rehearsal.py
python scripts/r7e_readonly_recon.py   # optional residual check
```

## Dry (clean worktree)

```bash
python -m tyrex_pm.application.cli r7b-live-once \
  --strategy reference-momentum \
  --market-family btc_updown_5m \
  --max-windows 3 \
  --max-buy-collateral 5.00 \
  --dry-run \
  --output-dir var/reporting/r7f_dry
```

## Live (operator only — one lifecycle)

```bash
cd /e/polymarket/Tyrex_PM && \
test -z "$(git status --porcelain)" && \
python -m tyrex_pm.application.cli r7b-live-once \
  --strategy reference-momentum \
  --market-family btc_updown_5m \
  --max-windows 3 \
  --max-buy-collateral 5.00 \
  --execute-live \
  --output-dir var/reporting/r7f_third_live
```

### Expected paths

- `var/reporting/r7f_third_live/report_<run_id>.json`
- `var/reporting/r7f_third_live/facts_<run_id>.jsonl`
- `var/reporting/r7f_third_live/budget_<run_id>.json` (if written)
- `var/state/r7/lifecycle_residuals.json`
- `var/state/r7/position_acknowledgment.json`

### Acceptable terminals

`FLAT` · `FLAT_WITH_DUST` · `FLAT_EXTERNAL_ACTION`

### Manual fallback

If `MANUAL_INTERVENTION` / `RESIDUAL_EXPOSURE`: do **not** re-run live. Flatten in UI if sellable; send report + facts + residuals JSON.

### Reminder

FAK SELL uses a minimum acceptable bid-side price; a moved book may no-match — runtime replans with a fresh fingerprint up to 3 attempts, then stops.
