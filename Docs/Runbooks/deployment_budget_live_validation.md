# Fresh live run — validate deployment-budget risk

One controlled **live** session to confirm caps use **deployment** (pending `leaves ×` limit + filled `abs(qty) × avg_px_open`), not the removed marked-portfolio path.

**Operating model:** Caps reflect **Nautilus `Cache` + `Portfolio`**, not wallet cash. Use **one bot per wallet**; see [OPERATIONS.md](../OPERATIONS.md) § *Current status & operating model* and [README.md](../README.md) § *Validation & evidence*.

## 0. Pre-checks

```powershell
cd e:\polymarket\Tyrex_PM
python scripts/verify_polymarket_auth.py
```

Ensure `.env` has production L2 credentials. Do **not** commit secrets.

Confirm risk YAML has **no** obsolete keys (loader errors if present):

- `max_order_quantity`, `portfolio_sizing_mode`, `fail_on_unresolved_portfolio_exposure`, `fail_on_unresolved_position_for_token_cap`

## 1. Config paths (live deployment-budget check)

**Bundled scenarios:** `config/scenarios/shadow_validation/` is **shadow-only** (safe compose). `config/scenarios/live_validation/` is a **live** template with isolated state. For this **live** runbook, use a live runtime plus a risk profile that enables B2/B3 gates:

| Role | Path |
|------|------|
| Strategy | `config/scenarios/shadow_validation/guru_follow.yaml` or `config/scenarios/live_validation/guru_follow.yaml` (replace `guru_wallet_address` if needed) |
| Risk | `config/risk/guru_follow_risk_phaseb_b2_b3_validate.yaml` — finite portfolio + concurrent rests |
| Runtime | `config/runtime/live_polymarket.yaml` or `config/scenarios/live_validation/live_polymarket.yaml` — **`execution_mode: live`**, `reporting_enabled: true` |

For **shadow / smoke** only (no deployment budget wiring), use all three files under `config/scenarios/shadow_validation/` as documented in that folder’s `README.md`.

**Optional:** temporarily lower `max_notional_usd_per_order` and set `max_notional_policy: deny` in a **copy** of the risk file to force a cheap per-order denial — then restore.

## 2. Launch the run

From repo root (PowerShell):

```powershell
cd e:\polymarket\Tyrex_PM
python scripts/run_guru.py `
  --strategy-conf config/scenarios/shadow_validation/guru_follow.yaml `
  --risk-conf config/risk/guru_follow_risk_phaseb_b2_b3_validate.yaml `
  --live-conf config/runtime/live_polymarket.yaml `
  --log-name deploy-budget-check `
  --reporting-run-id deploy-budget-check-01
```

Let it run long enough for at least one guru signal and risk evaluation (often 30–120 seconds depending on activity). Stop with **Ctrl+C** when done.

## 3. Logs to inspect (immediate)

| File | Confirm |
|------|---------|
| `logs/live/deploy-budget-check_tyrex.log` | Line `tyrex_pm phase_b:` includes `deployment_budget_wired=True`, `portfolio_deployment_cap_usd=`, `fail_on_unresolved_portfolio_deployment=`, `fail_on_unresolved_token_deployment=`. **No** `b1_aggregator_wired` or `portfolio_notional_cap_usd`. |
| Same file | Any deny: `event=tyrex_risk_ops` with `gate=token_deployment_cap`, `portfolio_deployment_cap`, `portfolio_deployment_unresolved`, `order_deployment_cap` (legacy per-order strings may still map here), `reserve`, `guru_concurrent`, etc. |
| `logs/live/deploy-budget-check_nautilus.log` | `copy_skip` with `risk_denied` and `risk_detail=` matching **`RISK_*_DEPLOYMENT_*`** when caps fire — not `risk_portfolio_exposure_unresolved` on **new** config unless reading stale telemetry labels. |

## 4. Reporting artifacts

Run folder:

`var/reporting/runs/deploy-budget-check-01/`

Expected: `facts.jsonl`, `manifest.json` (and optionally `run.sqlite` if built).

**Summarize:**

```powershell
cd e:\polymarket\Tyrex_PM
python -m tyrex_pm.reporting summarize --run-dir var/reporting/runs/deploy-budget-check-01 --build-db
```

Inspect **`summary.json`** / **`summary.md`**:

- **`risk_impact.risk_uses_deployment_budget`**: should be **`true`**.
- **`risk_impact.deployment_budget_count`**: **> 0** after at least one risk eval on live with budget wired.
- **`risk_decision`** rows in JSONL (or SQLite): fields such as **`order_deploy_usd_at_eval`**, **`token_deploy_at_eval`**, **`portfolio_deploy_at_eval`**, **`deployment_budget`** / **`deployment_budget_wired`** — **not** `e_portfolio` / B1 snapshots.

## 5. Signals that confirm the new model

**Should appear (live):**

- `tyrex_pm phase_b: ... deployment_budget_wired=True ...`
- Reason codes: `risk_order_deployment_exceeded`, `risk_token_deployment_exceeded`, `risk_portfolio_deployment_exceeded`, `risk_token_deployment_unresolved`, `risk_portfolio_deployment_unresolved` (when strict flags bite).
- Reporting: `deployment_budget` fact type and deployment fields on `risk_decision`.

**Should not appear as *active architecture* (new runs):**

- Log keys: `b1_aggregator_wired`, `e_portfolio`, `b1_complete`, `portfolio_sizing_mode`.
- Config loaded successfully with obsolete risk keys (load **fails** instead).
- Legacy multi-scenario folders under `config/scenarios/` — replaced by **`shadow_validation`** / **`live_validation`** plus `config/risk/*_phaseb_*` profiles.

**Legacy enum only (old artifacts / old summarize rows):**

- `risk_portfolio_exposure_unresolved`, `risk_portfolio_notional_cap_exceeded` may still exist in **historical** JSONL; new denials should prefer **`RISK_*_DEPLOYMENT_*`** per [`reason_codes.py`](../../src/tyrex_pm/core/reason_codes.py).

## 6. Shadow control run (optional)

Shadow does **not** wire `deployment_budget`; finite portfolio cap is **rejected at compose**. For a quick “still boots” check:

```powershell
python scripts/run_guru.py `
  --strategy-conf config/scenarios/shadow_validation/guru_follow.yaml `
  --risk-conf config/scenarios/shadow_validation/guru_follow_risk.yaml `
  --live-conf config/scenarios/shadow_validation/live_polymarket.yaml
```

The bundled **`shadow_validation`** runtime is **`execution_mode: shadow`** with a **shadow-safe** risk file (no finite portfolio / B3 / B4 compose errors).

---

**See also:** [phase_b_operational_validation.md](../Implementation/phase_b_operational_validation.md), [OPERATIONS.md](../OPERATIONS.md).
