# Module: `tyrex_pm.config`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md) · [CONFIG_MODEL](../../CONFIG_MODEL.md)

## A. Role

**Validate and load** non-secret configuration for the operational guru path: **strategy**, **risk**, and **runtime** YAML files.

## B. Boundaries

**Belongs here:** Dataclasses + `load_*_settings(path)` functions; validation errors as `ValueError` with file context.

**Does not belong here:** Secrets, `.env` parsing (handled in `scripts/run_guru.py`), or business rules like “should we trade this token?” (that is `signal/` + `strategy`).

## C. Internal structure (implemented)

| File | Contents |
|------|----------|
| `loaders.py` | `StrategySettings`, `RiskSettings`, `RuntimeSettings`; `load_strategy_settings`, `load_risk_settings`, `load_runtime_settings`. |
| `__init__.py` | Re-exports loaders and types. |

## D. Main interactions

- **runtime:** `guru_compose.build_guru_trading_node` accepts the three settings objects.
- **risk:** `ConfiguredRiskPolicy` takes `RiskSettings`.
- **execution:** `PolymarketExecutionPolicy` holds `RuntimeSettings` (e.g. host/chain); secrets still from env via `clob_factory`.

## E. Status

**Implemented** for v1 split YAML. Legacy `core.app_config` remains for older examples.

## F. Extension guidance

- Add new fields as **optional with defaults** when possible; fail loud on invalid combinations.
- Never read private keys or API secrets in this module.
- After changing loaders, update [CONFIG_MODEL.md](../../CONFIG_MODEL.md) and add/adjust tests in `tests/test_split_config_loaders.py`.
