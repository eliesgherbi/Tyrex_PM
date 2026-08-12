# Current documentation

These documents describe the **supported** runtime and operator surface. If they disagree with code or tests, trust the code and correct this folder.

| Document | Use it for |
|----------|------------|
| [Architecture](architecture.md) | Ownership, composition, clocks, safety, reporting |
| [Strategy inputs](strategy_inputs.md) | Fact catalog, `InputContract`, scheduler, live vs unimplemented adapters |
| [Execution lifecycle](execution_lifecycle.md) | Entry / exit / recovery, intents, maker vs taker |
| [Position protection](protection.md) | SL / TP / trailing overlay (`protection:` block) |
| [Running and recovery](running.md) | YAML configs, CLI, outcomes, recovery |
| [Adding a strategy](extending.md) | Plugin checklist; what not to fork |

Package version: `0.3.0` (`tyrex-pm`).

## What is live today

Two registered strategy plugins share one host:

- **`z_gap`** — Chainlink PTB + Binance spot trades + Polymarket books; strategy-owned exits.
- **`ask70`** — Polymarket books + clock + account; entry-only harness; **requires** a `protection:` block.

The host starts **only** connectors in the strategy’s input-contract closure. Binance and Chainlink are not universal liveness dependencies.

Historical milestone reports under `Docs/implementation/` and numbered specs under `Docs/specifications/` are evidence or design baselines, not current API.
