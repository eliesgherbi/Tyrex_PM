# Developer guide — `tyrex_pm.config`

[README](README.md) · [CONFIG_MODEL](../../CONFIG_MODEL.md) · [LIVE_ARCHITECTURE](../../LIVE_ARCHITECTURE.md)

## Responsibility

Parse and validate **strategy**, **risk**, and **runtime** YAML into typed settings dataclasses (`StrategySettings`, `RiskSettings`, `RuntimeSettings`). **No secrets** — never read `.env` here.

## Main file

- **`loaders.py`** — field coercion, obsolete-key rejection, **compose-time contracts** (e.g. shadow cannot enable live-only deployment gates; reserve requires capital gate).

## Validation philosophy

- **Fail loud:** `ValueError` with file path and field context.
- **Obsolete keys:** explicit raise (no silent ignore) for removed risk/runtime keys — guides operators to deployment-budget model docs.

## Extension workflow

1. Add field to dataclass + loader branch + default.
2. Update **`Docs/CONFIG_MODEL.md`**.
3. Add tests in `tests/test_split_config_loaders.py`.
4. If compose behavior changes, update `runtime/guru_compose.py` and **`Docs/OPERATIONS.md`** if operator-visible.

## Pitfalls

- **Flat YAML:** only `token_filter` is nested; loaders expect top-level keys on risk/runtime.
- **`framework_phase_b_eligible` / `phase_b_framework_truth_gates_active`**: gate **live-only** risk YAML that needs deployment-budget readers (portfolio cap, concurrent guru rests) — **not** the Tier A/Tier B split; naming is historical. Keep in sync with `guru_compose` + **`validate_phase_b_runtime_contract`**.
