# Developer guide — `tyrex_pm.runtime`

[README](README.md) · [Architecture](../../Architecture.md)

## Responsibility

**Composition root:** build a **`TradingNode`** with guru ingest actors, `CopyStrategy`, injected risk readers, execution port, and optional reporting context. Enforce **valid combinations** of `execution_mode` + risk YAML (shadow vs live-only gates).

## Core entrypoint

- **`build_guru_trading_node`** (`guru_compose.py`) — the only supported production assembly for guru follow.

## Key collaborators

| Module | Role |
|--------|------|
| `state_readers.py` | Nautilus `Cache` / `Portfolio` / order views; guru resting detection; allowance provider; **instrument_id_for_outcome_token**. |
| `deployment_budget.py` | **`NautilusDeploymentBudget`** — pending + filled USD for risk. |
| `phase_b_startup.py` | Formats the **`tyrex_pm phase_b:`** INFO log line (compose summary of gate wiring). |
| `guru_instrument_dynamic.py` | Dynamic instrument activation from guru tokens + Gamma/CLOB. |
| `guru_cache_warmup.py` | Optional proactive `Cache` seed from guru activity. |
| `polymarket_nautilus_env.py`, `clob_factory.py` | Env → Nautilus factories / **py-clob** client for collateral reads (not parallel order submit). |

## Compose invariants

- **`load_state=False`, `save_state=False`** on `TradingNodeConfig` — restart behavior documented in Architecture / current_state.
- **Shadow:** no Polymarket exec client; risk cannot use deployment readers that require live framework for **finite portfolio / concurrent / reserve** combos — **fail fast at compose**.
- **Live:** register data + exec clients; wire **`NautilusGuruExecutionPort`**; inject **`NautilusDeploymentBudget`** when eligibility predicates match.

## Extension patterns

- **New actor:** register in `guru_compose` alongside monitor/stream; publish **same** `GuruTradeSignal` contract unless versioning bus topic.
- **New reader for risk:** add implementation in `state_readers.py`, inject in compose, consume in `ConfiguredRiskPolicy` — keep strategy ignorant.

## Pitfalls

- **Dual submit paths:** live guru orders must go through **`NautilusGuruExecutionPort`** only.
- **`phase_b` log label** is a **message prefix** for compose summary, not a separate runtime mode name.

## Tests

`tests/test_guru_compose_build.py`, integration tests touching compose validation.
