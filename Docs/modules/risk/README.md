# Module: `tyrex_pm.risk`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md) · [CONFIG_MODEL](../../CONFIG_MODEL.md) · **[Current state](../../Implementation/current_state.md)**

## A. Role

**Pre-trade gates:** given an `OrderIntent`, approve or reject with a reason string suitable for logs and automation.

## B. Boundaries

**Belongs here:** `RiskPolicy` protocol, concrete policies (`ShadowAllPassRisk`, `ConfiguredRiskPolicy`), **exposure / capital logic** that risk owns.

**Does not belong here:** Building `OrderIntent` (strategy), HTTP/CLOB (execution), guru polling (data). **No** `Cache` imports in policy code — readers are **injected** from `runtime/guru_compose`.

## C. Internal structure (implemented)

| File | Contents |
|------|----------|
| `policy.py` | `RiskPolicy` `Protocol`, `ShadowAllPassRisk`. |
| `configured.py` | **`ConfiguredRiskPolicy`** — `RiskSettings`; optional readers + **B2** portfolio aggregator + **B3** `ExecutionStateReader.count_guru_resting_orders_open`; **B4** reserve in `_capital_gate_eval`; **capital gate**; per-token cap; **`note_fill_assumption`** legacy py-clob. |
| `__init__.py` | Exports. |

## D. Main interactions

- **strategy:** `CopyStrategy` calls `evaluate(intent)` before execution.
- **config:** `RiskSettings` from YAML (**includes Phase A capital fields**).
- **runtime:** injects reader implementations; legacy **`PolymarketExecutionPolicy`** may call `note_fill_assumption`.

## E. Status

**Operational:** Framework path uses **measured pending** (open orders, leaves) + **measured filled** (`net_exposure` when reader wired) + optional **capital gate** + **B4** reserve when configured. Operator **matrix / reason codes:** `Docs/OPERATIONS.md` § Phase B. **Partial / upstream:** position and account truth depend on **Nautilus + Polymarket adapter** updating `Portfolio` / account state.

## F. Extension guidance

- Implement `RiskPolicy` with the same `evaluate` signature; inject via `CopyStrategy.set_risk_policy`.
- Prefer **`ReasonCode`** / explicit strings over silent drops.
- **B4** (reserve) extends this module **without** moving logic into strategy or duplicating B1 aggregation; **B3** guru-ID logic stays in ``state_readers``. **B5** is documentation + compose startup summary only.
