# Development quick reference

**Full developer guide:** [developer_guide.md](developer_guide.md) · **Doc index:** [README.md](README.md) · **Architecture:** [Architecture.md](Architecture.md) · **Modules:** [modules/README.md](modules/README.md) · **Config:** [CONFIG_MODEL.md](CONFIG_MODEL.md)

## Setup

```bash
pip install -e ".[dev]"
```

Secrets: `.env` only — see `Docs/Runbooks/polymarket_operator_v1_00.md`.

## Tests

```bash
pytest tests/ -q
ruff check src tests scripts examples
```

Opt-in network test: `set TYREX_NETWORK_TESTS=1` then `pytest tests/test_resolution_network.py -v` (Windows).

## Where details live

Ownership boundaries, path matrix (framework vs legacy), extension points, and debugging order are in **[developer_guide.md](developer_guide.md)** — read that before large changes.
