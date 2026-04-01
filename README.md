# Tyrex_PM

Polymarket trading platform — planning and implementation per `Docs/Implementation/Project starter/`.

## Quick start (milestone v1.00)

1. Create a Python 3.10+ environment and install: `pip install -e ".[dev]"`
2. Configure credentials (see `Docs/Runbooks/polymarket_operator_v1_00.md`):
   - **Either** copy `.env.example` to `.env` at the **repo root** and fill values,
   - **Or** export the same variable names in your shell.
   - **Precedence:** shell / process environment **overrides** `.env` for any given key.
3. From repo root: `python scripts/verify_polymarket_auth.py`

Do not commit `.env` or evidence containing secrets (`.env` is gitignored).

## Package layout

Implementation lives under **`src/tyrex_pm/`** (not `platform`, to avoid clashing with the Python standard library).

## Guru follow bot (shadow first)

```bash
pip install -e .
python scripts/run_guru.py \
  --strategy-conf config/strategy/guru_follow.yaml \
  --risk-conf config/risk/guru_follow_risk.yaml \
  --live-conf config/runtime/live_polymarket.yaml
```

Set `execution_mode: live` in `config/runtime/live_polymarket.yaml` only after operator checks (`Docs/OPERATIONS.md`). Secrets stay in `.env`.

## Common commands

```bash
pip install -e ".[dev]"
ruff check src tests scripts
pytest tests/ -q                           # unit tests (network test skipped)
set TYREX_NETWORK_TESTS=1 && pytest tests/test_resolution_network.py -v   # Windows: opt-in Polymarket HTTP
python scripts/resolve_markets.py --write-notes Docs/validation/v1_01_resolution_notes.md
```

See `Docs/dependency_lock.md` for versions used in development.

## Documentation

- **Developers:** [Docs/DEVELOPMENT.md](Docs/DEVELOPMENT.md) — modules, tests, `run_guru` wiring.
- **Config fields:** [Docs/CONFIG_MODEL.md](Docs/CONFIG_MODEL.md) — strategy / risk / runtime YAML.
- **Operators:** [Docs/OPERATIONS.md](Docs/OPERATIONS.md) — env, run commands, shadow vs live, logs.
- **Runbooks:** `Docs/Runbooks/` (auth, order smoke, live stub).
