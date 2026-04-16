# Developer guide — `tyrex_pm.risk`

[README](README.md) · [Architecture](../../Architecture.md) · [CONFIG_MODEL](../../CONFIG_MODEL.md) · [LIVE_ARCHITECTURE](../../LIVE_ARCHITECTURE.md)

## Responsibility

**Pre-trade authorization:** given an `OrderIntent` (token, side, qty, `price_ref`, correlation id), return **approve** with possibly **adjusted quantity** (per-order clip/bump policies) or **reject** with a stable reason string (`ReasonCode` / policy text) suitable for logs and reporting.

## Position in the pipeline

```
CopyStrategy → OrderIntent → ConfiguredRiskPolicy.evaluate → ExecutionPort
```

Risk runs **after** strategy sizing and **before** any venue submit. It must **not** decide whether the guru trade “counts” as a copy candidate (that is `signal/` + strategy). It **does** decide economic caps, collateral, and deployment accounting using **injected readers** — on **live** with wallet sync, **Tier A** (**VenueState**) for deployment inputs; **Tier B** (Nautilus) only when Tier A is not wired.

## Core types

- **`RiskPolicy`** (`policy.py`) — protocol: `evaluate(intent) -> (bool, reason)`.
- **`ConfiguredRiskPolicy`** (`configured.py`) — production implementation: reads **`RiskSettings`**, uses injected **readers** and optional **`NautilusDeploymentBudget`**.
- **`NautilusDeploymentBudget`** (`runtime/deployment_budget.py`) — pending USD + filled USD per token and portfolio; **filled** uses **venue size × mark** when **`VenueState`** is set, else Nautilus **avg_px_open × qty**.

## Injected dependencies (live)

Compose (`runtime/guru_compose.py`) injects:

- Execution state reader (open orders, guru client order id tagging).
- Position / portfolio readers for **filled** deployment.
- Optional allowance / account snapshot providers for **capital gate** and **collateral reserve**.
- **`NautilusDeploymentBudget`** instance when `execution_mode == live` and deployment wiring is valid.

**Invariant:** Risk implementation code must **not** import `Cache` or `Portfolio` directly — only use injected interfaces (tests mock these).

## Main evaluation stages (conceptual)

1. **Kill switch / missing price** (if configured).
2. **Per-order deploy** vs `min_*` / `max_*` notional and **`deny` vs `cap`** policies — may clip or bump qty.
3. **Per-token / portfolio deployment caps** (live, when finite caps set).
4. **Concurrent guru resting orders** (live, when cap set).
5. **Capital gate / reserve** (live, when enabled — py-clob balance vs reserve + intent notional).

Shadow mode: compose **rejects** YAML that requires live-only readers (finite portfolio cap, concurrent cap, positive reserve) so operators do not get silent wrong risk.

## Reporting hooks

When `RunContext` is active, `configured.py` emits **`risk_decision`**, **`account_snapshot`**, and related capital fields — see **`Docs/reporting_fact_model.md`**. Keep emits **thin**: assemble metrics in dedicated helpers (`_capital_metrics_for_facts`).

## Extension patterns

- **New gate:** add a checked block inside `evaluate` (or a small private method), return a **`ReasonCode`** value or explicit string, and document it in `OPERATIONS.md` if operator-facing.
- **New reader:** extend `runtime/state_readers.py` and inject via `guru_compose` — avoid strategy touching the reader.
- **Do not** duplicate deployment math outside `NautilusDeploymentBudget` for cap checks.

## Pitfalls

- **Mark vs deploy:** caps are **not** full portfolio mark-to-market; **pending** uses limit × leaves; **filled** with Tier A uses **venue × mark** (fallback + fact if mark missing), else **avg_px_open × qty** from Nautilus positions.
- **Unresolved positions:** strict flags can deny with `RISK_*_DEPLOYMENT_UNRESOLVED` until `Portfolio` is usable — operational, not necessarily “wrong YAML.”
- **Min notional in risk vs venue min quantity:** risk floors are **USD deploy**; venue **min_quantity** is enforced later — may yield **`exec_instrument_quantize_skip`** without a risk deny.

## Tests

`tests/unit/test_configured_risk.py`, `tests/test_phase_b_*.py` (deployment-budget scenarios).
