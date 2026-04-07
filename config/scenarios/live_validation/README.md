# Live validation scenario (`live_validation`)

**Purpose:** bundled **strategy + risk + runtime** for a **controlled live** check (real `execution_mode: live`, Polymarket exec clients). Requires working `.env` (e.g. `POLYMARKET_PK`, funder) — verify with `python scripts/verify_polymarket_auth.py`.

| File | Role |
|------|------|
| `guru_follow.yaml` | Strategy — replace `guru_wallet_address` if needed |
| `guru_follow_risk.yaml` | Risk — same **small-cap** profile as shadow validation (`max_notional_usd_per_order: 10`, `cap`); safe for a short smoke. For deployment-budget / B2–B3 drills, pass `--risk-conf config/risk/guru_follow_risk_phaseb_b2_b3_validate.yaml` instead. |
| `live_polymarket.yaml` | Runtime — **`execution_mode: live`**, dynamic instruments on, state under `var/scenarios/live_validation/` |

**Git Bash (repo root):**

```bash
cd /e/polymarket/Tyrex_PM
python scripts/run_guru.py \
  --strategy-conf config/scenarios/live_validation/guru_follow.yaml \
  --risk-conf config/scenarios/live_validation/guru_follow_risk.yaml \
  --live-conf config/scenarios/live_validation/live_polymarket.yaml \
  --log-name live-validation-smoke \
  --reporting-run-id live-validation-smoke-01
```

Logs: `logs/live/live-validation-smoke_tyrex.log`, `logs/live/live-validation-smoke_nautilus.log`. Reporting: `var/reporting/runs/live-validation-smoke-01/`.

**Stop with Ctrl+C** when done. For deployment-budget validation specifically, follow `Docs/Runbooks/deployment_budget_live_validation.md` (swap risk file as documented there).
