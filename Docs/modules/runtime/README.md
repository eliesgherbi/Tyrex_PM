# Module: `tyrex_pm.runtime`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md)

## A. Role

**Wire** configuration and Tyrex components into a runnable **Nautilus `TradingNode`**: guru actor + copy strategy + risk + execution ports. Provide **`ClobClient`** construction from environment (and optional runtime host/chain overrides).

## B. Boundaries

**Belongs here:** Composition roots, factory functions used by `scripts/run_guru.py`, optional smoke stubs.

**Does not belong here:** Individual policy logic (use `signal/` / `risk/`), Data API parsing (`data/`), or CLI argument parsing (scripts).

## C. Internal structure (implemented)

| File | Contents |
|------|----------|
| `guru_compose.py` | **`build_guru_trading_node(strategy, risk, runtime)`** — builds node, actor, strategy, wires ports. |
| `clob_factory.py` | **`build_clob_client_from_env(runtime=None)`** — secrets from env; optional `RuntimeSettings` for `clob_host` / `chain_id`. |
| `live_stub.py` | Legacy / opt-in live smoke placeholder. |
| `__init__.py` | Minimal package marker. |

## D. Main interactions

- **config:** consumes the three settings types.
- **data:** instantiates `GuruMonitorActor`.
- **strategy / risk / execution:** instantiates and injects dependencies.

## E. Status

**Primary entry:** `guru_compose` + `run_guru.py`.

## F. Extension guidance

- Add new runners (e.g. backtest) as **new compose functions** that reuse the same strategy/risk/execution types where possible.
- Keep `TradingNodeConfig` differences (clients, clocks) explicit — avoid hiding env side effects inside strategies.
