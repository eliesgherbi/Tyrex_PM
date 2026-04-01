# Module: `tyrex_pm.core`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md)

## A. Role

Shared **domain primitives** and **stable identifiers** used across data, strategy, risk, and execution without importing Nautilus or HTTP clients.

## B. Boundaries

**Belongs here:** `GuruTradeSignal`, `OrderIntent`, `ReasonCode`, small cross-cutting helpers (logging config, legacy single-file app config).

**Does not belong here:** API clients, YAML loaders for the three-way split (`config/loaders.py`), Nautilus actors/strategies, or `py-clob` types.

## C. Internal structure (implemented)

| File | Contents |
|------|----------|
| `types.py` | `GuruTradeSignal`, `OrderIntent` dataclasses — contract between `data` and `strategy`. |
| `reason_codes.py` | `ReasonCode` `StrEnum` — log and skip reasons (risk, live errors, allowlist, …). |
| `app_config.py` | Older monolithic YAML loader (`mode`, `trader_id`, …) for non-`run_guru` tools. |
| `logging_config.py` | Logging helpers if present for shared setup. |
| `market_types.py` | Shared market-related types used by data/resolution paths. |

## D. Main interactions

- **data** / **strategy:** serialize and consume `GuruTradeSignal`.
- **strategy** / **risk** / **execution:** build and pass `OrderIntent`; risk returns reason strings aligned with `ReasonCode`.
- **tests:** assert stable reason codes without importing heavy stacks.

## E. Status

**Solid:** `types`, `reason_codes`.

**Legacy but real:** `app_config` still used where a single YAML is enough; operational guru path uses `config.loaders` instead.

## F. Extension guidance

- Add new fields to `GuruTradeSignal` / `OrderIntent` only when **all** consumers (parse, strategy, risk, execution) agree — version or document breaking changes.
- Add new `ReasonCode` values for new reject paths; avoid free-form strings in hot paths.
- Keep this package **import-light** (no `httpx`, no `nautilus_trader` if avoidable).
