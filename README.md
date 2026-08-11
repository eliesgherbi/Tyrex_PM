# Tyrex_PM

Tyrex_PM is an event-driven Polymarket trading framework. The production path is intentionally singular: market data and account readiness feed a strategy driver; typed intents enter one execution lifecycle; one async gateway owns all authenticated venue I/O; and a durable reducer decides whether the session is flat, no-fill, exposed, or requires manual intervention.

Z-Gap is the first strategy implementation and the reference integration for future strategies.

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

pytest -q
```

## Run live

This can submit real orders. Review the single run config and account allowances first.

```bash
python -m tyrex_pm.application.cli run \
  --config config/runs/z_gap_tiny_live.yaml \
  --dotenv .env \
  --live
```

`--live` is the explicit process-level mutation authorization. Without it, the runtime refuses to start. Every run writes `run_summary.json`; execution authority is persisted in SQLite under the configured state directory.

See [current documentation](Docs/latest/README.md).
