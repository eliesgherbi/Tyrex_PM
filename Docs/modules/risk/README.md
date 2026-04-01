# Module: `tyrex_pm.risk`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md) · [CONFIG_MODEL](../../CONFIG_MODEL.md)

## A. Role

**Pre-trade gates:** given an `OrderIntent`, approve or reject with a reason string suitable for logs and automation.

## B. Boundaries

**Belongs here:** `RiskPolicy` protocol, concrete policies (`ShadowAllPassRisk`, `ConfiguredRiskPolicy`), exposure bookkeeping that **only** risk should own.

**Does not belong here:** Building `OrderIntent` (strategy), HTTP/CLOB (execution), or guru polling (data).

## C. Internal structure (implemented)

| File | Contents |
|------|----------|
| `policy.py` | `RiskPolicy` `Protocol`, `ShadowAllPassRisk` (tests / harnesses). |
| `configured.py` | `ConfiguredRiskPolicy` — reads `RiskSettings`; fail-closed qty/notional/kill-switch; `note_fill_assumption` for session exposure after live submit. |
| `__init__.py` | Exports policies. |

## D. Main interactions

- **strategy:** `CopyStrategy` calls `evaluate(intent)` before execution.
- **config:** `RiskSettings` from YAML.
- **execution:** `PolymarketExecutionPolicy` may call `on_submit_ok=intent -> note_fill_assumption` (wired in `runtime/guru_compose.py`).

## E. Status

**Operational:** `ConfiguredRiskPolicy` is fail-closed with `ReasonCode` alignment.

**Known limit:** exposure is **session estimate** after successful submits — not live venue positions.

## F. Extension guidance

- Implement `RiskPolicy` with the same `evaluate` signature; inject via `CopyStrategy.set_risk_policy`.
- Prefer **explicit reason codes** (`ReasonCode` / strings) over silent drops.
- Heavy portfolio queries belong in a **dedicated cache or adapter** that `RiskPolicy` reads — keep the policy class testable.
