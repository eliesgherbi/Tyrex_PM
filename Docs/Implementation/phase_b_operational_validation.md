# Phase B — operational validation (deployment budget)

**Purpose:** Short checklist for **live** runs after config changes, restarts, or before widening size.  
**Canonical risk model:** [CONFIG_MODEL.md](../CONFIG_MODEL.md) § Risk — **deployment budget** only (pending + filled cost basis).  
**Step-by-step CLI:** [Runbooks/deployment_budget_live_validation.md](../Runbooks/deployment_budget_live_validation.md).

## Preconditions

- `execution_mode: live` with valid `.env` (see `scripts/verify_polymarket_auth.py`).
- Phase B framework gates (finite portfolio cap, concurrency cap, or B4 reserve) are **invalid** in shadow — use live runtime YAML when testing those paths.
- Obsolete risk YAML keys (`max_order_quantity`, `portfolio_sizing_mode`, `fail_on_unresolved_portfolio_exposure`, …) **must not** appear — the loader raises.

## What to verify

1. **Startup line** — After compose, logs contain `tyrex_pm phase_b:` with `deployment_budget_wired=true` on live, and `portfolio_deployment_cap_usd=` / `fail_on_unresolved_portfolio_deployment=` as configured. No `b1_aggregator_wired` or `portfolio_notional_cap_usd` (removed).
2. **Denials** — Cap breaches use **`RISK_*_DEPLOYMENT_*`** reason codes and `tyrex_risk_ops` gates like `token_deployment_cap`, `portfolio_deployment_cap`, `portfolio_deployment_unresolved`. Old **active** logs should not show B1 / `e_portfolio` / `portfolio_exposure` aggregation.
3. **Reconciliation** — After restart (`load_state=False`), expect brief ambiguity; persistent `RISK_PORTFOLIO_DEPLOYMENT_UNRESOLVED` or `RISK_TOKEN_DEPLOYMENT_UNRESOLVED` (strict flags) means positions/orders are not yet readable — not “wrong cap math” by itself.

## Related docs

- [OPERATIONS.md](../OPERATIONS.md) — modes, reason cheat sheet, reporting.
- [log_validation_playbook.md](../log_validation_playbook.md) — grep Q2–Q4.
- [current_state.md](current_state.md) — architecture snapshot.
