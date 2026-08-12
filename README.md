# Tyrex_PM

Tyrex_PM is an event-driven Polymarket trading framework. Strategies declare an input contract and emit typed intents. One host owns market ingestion, readiness, execution, protection, reconciliation, persistence, and reporting. One async gateway owns authenticated venue I/O. A durable reducer decides whether the session is flat, no-fill, exposed, or requires manual intervention.

Two live plugins share that host: **z_gap** (Chainlink PTB + Binance spot trades + books) and **ask70** (books-only entry harness with a required protection overlay). Connectors start only if the strategy contract needs them.

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

## Run live

This can submit real orders. Review the run config and account allowances first. `--live` is the explicit process-level mutation authorization.

```bash
python -m tyrex_pm.application.cli run \
  --config config/runs/z_gap_tiny_live.yaml \
  --dotenv .env \
  --live
```

Every run writes `run_summary.json`; execution authority is persisted in SQLite under the configured state directory.

See [current documentation](Docs/latest/README.md).
