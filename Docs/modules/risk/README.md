# Module: `tyrex_pm.risk`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md) · [CONFIG_MODEL](../../CONFIG_MODEL.md) · **[LIVE_ARCHITECTURE](../../LIVE_ARCHITECTURE.md)** · **[Current state](../../Implementation/current_state.md)** · **[DEVELOPER.md](DEVELOPER.md)**

## A. Role

**Pre-trade gates:** given an `OrderIntent`, approve or reject with a reason string suitable for logs and automation.

## B. Boundaries

**Belongs here:** `RiskPolicy` protocol, concrete policies (`ShadowAllPassRisk`, `ConfiguredRiskPolicy`), **exposure / capital logic** that risk owns.

**Does not belong here:** Building `OrderIntent` (strategy), HTTP/CLOB (execution), guru polling (data). **No** direct `Cache` / `Portfolio` / **`VenueState`** imports in policy code — readers are **injected** from `runtime/guru_compose`.

## C. Internal structure (implemented)

| File | Contents |
|------|----------|
| `policy.py` | `RiskPolicy` `Protocol`, `ShadowAllPassRisk`. |
| `configured.py` | **`ConfiguredRiskPolicy`** — `RiskSettings`; injected readers (**Tier A** when **`VenueState`** wired) + **`NautilusDeploymentBudget`** + concurrent guru rests; **`CapitalStateProvider`** (`runtime/capital`) for gate + reporting metrics; **reporting:** `_capital_metrics_for_facts`, **`account_snapshot`** + enriched **`risk_decision`** when run sink is active. |
| `__init__.py` | Exports. |

## D. Main interactions

- **strategy:** `CopyStrategy` calls `evaluate(intent)` before execution.
- **config:** `RiskSettings` from YAML (**includes capital gate / reserve fields**).
- **runtime:** injects reader implementations from **`guru_compose`**.

## E. Status

**Operational:** **Live** deployment budget uses **`NautilusDeploymentBudget`** fed by injected **`state_readers`** — **Tier A** when **`VenueState`** is composed (venue resting orders + venue position × mark for filled leg), **Tier B** fallbacks when not (shadow / `wallet_sync_enabled: false`). Optional **capital gate** + **collateral reserve** via **`CapitalStateProvider`** (venue-sourced collateral when wired). **Capital vs logs:** with **`reporting_enabled`**, `_capital_metrics_for_facts` records **canonical USD balance**, raw CLOB strings, and trust/disagreement flags — see [**reporting_fact_model.md**](../../reporting_fact_model.md). Operator **matrix / reason codes:** `Docs/OPERATIONS.md` § Deployment-budget risk. **Session convergence:** Nautilus adapter still updates **`Cache` / `Portfolio`** over time — see [**LIVE_ARCHITECTURE**](../../LIVE_ARCHITECTURE.md) for what to trust for **caps** vs **order lifecycle**.

## F. Extension guidance

- Implement `RiskPolicy` with the same `evaluate` signature; inject via `CopyStrategy.set_risk_policy`.
- Prefer **`ReasonCode`** / explicit strings over silent drops.
- **Collateral reserve** extends this module **without** moving logic into strategy; guru concurrent-rest identity stays in ``state_readers``. **[DEVELOPER.md](DEVELOPER.md)** — evaluation stages and pitfalls.
