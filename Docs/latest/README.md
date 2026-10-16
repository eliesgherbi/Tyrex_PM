# Current documentation

These documents describe the **supported** runtime and operator surface. If they disagree with code or tests, trust the code and correct this folder.

| Document | Use it for |
|----------|------------|
| [Architecture](architecture.md) | Ownership, composition, clocks, safety, reporting, lab |
| [Strategy inputs](strategy_inputs.md) | Fact catalog, `InputContract`, scheduler, live vs unimplemented adapters |
| [Execution lifecycle](execution_lifecycle.md) | Entry / exit / recovery, intents, maker vs taker |
| [Position protection](protection.md) | SL / TP / trailing overlay (`protection:` block) |
| [Running and recovery](running.md) | YAML configs, CLI, pre-live `eval`, outcomes, recovery |
| [Config parameter guide](config_guide.md) | Role of every YAML key; lookalikes; how to add a field |
| [Adding a strategy](extending.md) | Per-module patterns; plugin checklist; lab fixture before `--live` |
| [Data warehouse](data_warehouse.md) | Separate collector, export, and scripted replay. Live qualification is still an operator gate |

Package version: `0.3.0` (`tyrex-pm`).

## How to read this folder

1. **Operator first run** — [running.md](running.md): `--validate-config`, then `eval` on a lab fixture, then `--live` with the $5 cap.
2. **What a YAML key does** — [config_guide.md](config_guide.md). Facts and adapters are **not** YAML; they live on the plugin ([strategy_inputs.md](strategy_inputs.md)).
3. **Who owns what** — [architecture.md](architecture.md). One host; strategies emit intents only.
4. **New strategy or host enhancement** — [extending.md](extending.md): which module to touch, what not to fork, and how to add `tests/fixtures/lab/<name>.json`.

## What is live today

Four registered strategy plugins share one host:

- **`z_gap`** — Chainlink PTB + Binance spot trades + Polymarket books; strategy-owned exits.
- **`ask70`** — Polymarket books + clock + account; entry-only harness; **requires** a `protection:` block.
- **`feature_probe`** — books + Binance spot L2 imbalance; seam proof. Shipped catalog YAML is validate-only (`emit_entry: false`).
- **`settlement_value`** — buy expected settlement payout, hold to resolution. **Research only**: every live run profile is rejected until the hold/resolution lifecycle exists. It runs offline through the closed-loop strategy replay (`research/strategy_replay`, notebooks in `research/strategy_lab`). Plan: [research-to-live plan](../implementation/strategies/research_to_live_plan.md).

The host starts **only** connectors in the strategy’s input-contract closure. Binance and Chainlink are not universal liveness dependencies. Binance **spot L2** (and derived mid) is a live adapter; `feature_probe` names it, ask70/z_gap do not. Perp trades/L2/funding stay fail-closed.

`q_edge` is a guide contract only — not a registered plugin.

Historical milestone reports under `Docs/implementation/` and numbered specs under `Docs/specifications/` are evidence or design baselines, not current API.

First-release R1–R5 behavior (recovery-only MANUAL flatten, shared cutoff clock, BUY-only `features_ready`, z_gap lab packs, reproducibility) is described in the pages above. `--continue` (R6) and the live campaign (R7) are not operator steps on this revision. L2 / `feature_probe` consume-time freshness is deferred as F-L2.
