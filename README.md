# Tyrex_PM

Tyrex_PM is an event-driven Polymarket trading framework. Strategies declare an input contract and emit typed intents. One host owns market ingestion, readiness, execution, protection, reconciliation, persistence, and reporting. One async gateway owns authenticated venue I/O. A durable reducer decides whether the session is flat, no-fill, exposed, or requires manual intervention.

Four strategy plugins share that host and one driver contract: **settlement_value** (expected settlement value, hold to resolution or managed exit), **z_gap** (Chainlink PTB + Binance spot trades + books), **ask70** (books-only entry harness with a required protection overlay) and **feature_probe** (input check). Connectors start only if the strategy contract needs them. The same host components run live, paper, signal-only observation and research replay of recorded data.

A local **research workspace** (`tools/start_ihm.ps1`, http://127.0.0.1:4173/) records market data, builds window sets and replays any strategy on them. It places no orders. See [Docs/latest/research_workspace.md](Docs/latest/research_workspace.md).

## Install

```bash
python -m venv .venv
source .venv/Scripts/activate
pip install -e ".[dev]"
```

## Validate

```bash
python -m tyrex_pm.application.cli run \
  --config config/runs/z_gap_tiny_live.yaml \
  --validate-config

python -m tyrex_pm.application.cli run \
  --config config/runs/ask70_protection_tiny_live.yaml \
  --validate-config

pytest -q
```

## Paper and live

A paper config runs the live host against a simulated account (no signing):

```bash
python -m tyrex_pm.application.cli run --config config/runs/settlement_value_continuous_paper.yaml --paper
```

Live runs can submit real orders. Review the run config and account allowances first. `--live` is the explicit process-level mutation authorization.

```bash
python -m tyrex_pm.application.cli run \
  --config config/runs/z_gap_tiny_live.yaml \
  --dotenv .env \
  --live
```

Every run writes `run_summary.json`; execution authority is persisted in SQLite under the configured state directory.

See [current documentation](Docs/latest/README.md).
