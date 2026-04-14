# Module: `tyrex_pm.risk`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md) · [CONFIG_MODEL](../../CONFIG_MODEL.md) · **[Current state](../../Implementation/current_state.md)** · **[DEVELOPER.md](DEVELOPER.md)**

## A. Role

**Pre-trade gates:** given an `OrderIntent`, approve or reject with a reason string suitable for logs and automation.

## B. Boundaries

**Belongs here:** `RiskPolicy` protocol, concrete policies (`ShadowAllPassRisk`, `ConfiguredRiskPolicy`), **exposure / capital logic** that risk owns.

**Does not belong here:** Building `OrderIntent` (strategy), HTTP/CLOB (execution), guru polling (data). **No** `Cache` imports in policy code — readers are **injected** from `runtime/guru_compose`.

## C. Internal structure (implemented)

| File | Contents |
|------|----------|
| `policy.py` | `RiskPolicy` `Protocol`, `ShadowAllPassRisk`. |
| `configured.py` | **`ConfiguredRiskPolicy`** — `RiskSettings`; optional readers + **`NautilusDeploymentBudget`** (portfolio/token deployment caps) + concurrent guru rests; **`CapitalStateProvider`** (`runtime/capital`) for gate + reporting metrics; per-order / per-token / portfolio deployment caps; **reporting:** `_capital_metrics_for_facts`, **`account_snapshot`** + enriched **`risk_decision`** when run sink is active. |
| `__init__.py` | Exports. |

## D. Main interactions

- **strategy:** `CopyStrategy` calls `evaluate(intent)` before execution.
- **config:** `RiskSettings` from YAML (**includes capital gate / reserve fields**).
- **runtime:** injects reader implementations from **`guru_compose`**.

## E. Status

**Operational:** Framework path uses **deployment budget** — pending (`leaves ×` limit) + filled (`abs(qty) × avg_px_open`) — plus optional **capital gate** + **collateral reserve**. **Capital vs logs:** with **`reporting_enabled`**, `_capital_metrics_for_facts` records **canonical USD balance** (Nautilus cash preferred), raw CLOB strings, and trust/disagreement flags — see [**reporting_fact_model.md**](../../reporting_fact_model.md). Operator **matrix / reason codes:** `Docs/OPERATIONS.md` § Deployment-budget risk. **Upstream:** order and position snapshots depend on **Nautilus + Polymarket adapter** updating `Cache` / `Portfolio`.

## F. Extension guidance

- Implement `RiskPolicy` with the same `evaluate` signature; inject via `CopyStrategy.set_risk_policy`.
- Prefer **`ReasonCode`** / explicit strings over silent drops.
- **Collateral reserve** extends this module **without** moving logic into strategy; guru concurrent-rest identity stays in ``state_readers``. **[DEVELOPER.md](DEVELOPER.md)** — evaluation stages and pitfalls.
