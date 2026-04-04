# Module: `tyrex_pm.config`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md) · [CONFIG_MODEL](../../CONFIG_MODEL.md)

## A. Role

**Validate and load** non-secret configuration for the operational guru path: **strategy**, **risk**, and **runtime** YAML files.

## B. Boundaries

**Belongs here:** Dataclasses + `load_*_settings(path)` functions; validation errors as `ValueError` with file context.

**Does not belong here:** Secrets, `.env` parsing (handled in `scripts/run_guru.py`). **Token filter semantics** live in `signal.TokenFilterSpec`; YAML only supplies `enabled` + list.

## C. Internal structure (implemented)

| File | Contents |
|------|----------|
| `loaders.py` | `StrategySettings`, **`TokenFilterSettings`**, `RiskSettings`, `RuntimeSettings`; loaders; **Phase B B0:** `validate_phase_b_runtime_contract`, `phase_b_framework_truth_gates_active`. |
| `__init__.py` | Re-exports loaders and types. |

## D. Main interactions

- **runtime:** `guru_compose.build_guru_trading_node` accepts the three settings objects.
- **risk:** `ConfiguredRiskPolicy` takes `RiskSettings`.
- **execution:** `PolymarketExecutionPolicy` / **`NautilusGuruExecutionPort`** use `RuntimeSettings` as composed; secrets from env via `clob_factory` where applicable.

## E. Status

**Implemented** for v1 split YAML. Legacy `core.app_config` remains for older examples.

## F. Extension guidance

- Add new fields as **optional with defaults** when possible; fail loud on invalid combinations.
- Never read private keys or API secrets in this module.
- After changing loaders, update [CONFIG_MODEL.md](../../CONFIG_MODEL.md) and add/adjust tests in `tests/test_split_config_loaders.py`.
- **Phase B (B0):** framework-gate and reserve rules live in `loaders.py`; operator-facing **matrix** in [OPERATIONS.md](../../OPERATIONS.md) § Phase B.
