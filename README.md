# Tyrex_PM

Polymarket **guru-follow** trading stack on **NautilusTrader**: RTDS + Data API ingest, typed YAML config, deployment-based risk, and shadow or live execution via **`NautilusGuruExecutionPort`**.

**Operators:** Risk caps use **Nautilus framework truth** (open orders + position cost basis for deployment), **not** wallet cash. **One bot, one dedicated wallet** is the supported model today; manual/UI or other bots on the same wallet can desync framework state. See **[Docs/OPERATIONS.md](Docs/OPERATIONS.md)** § *Current status & operating model* and **[Docs/README.md](Docs/README.md)** for validation doc links.

## Quick start

1. Python 3.10+ and `pip install -e ".[dev]"`
2. Credentials: copy `.env.example` to **repo root** `.env` (or export the same variables). Shell overrides `.env` per key.
3. `python scripts/verify_polymarket_auth.py`

Do not commit `.env` or secrets.

## Guru follow (`run_guru.py`)

```bash
pip install -e .
python scripts/run_guru.py \
  --strategy-conf config/strategy/guru_follow.yaml \
  --risk-conf config/risk/guru_follow_risk.yaml \
  --live-conf config/runtime/live_polymarket.yaml
```

Set `execution_mode: live` in runtime YAML only after operator checks (**[Docs/OPERATIONS.md](Docs/OPERATIONS.md)**). Logs: `logs/<mode>/run_tyrex.log`, `run_nautilus.log`; optional `--log-name`. Reporting: runtime `reporting_enabled` + `--reporting-run-id`. Optional **Layer A** signal filters: top-level **`token_filter`** unchanged; add **`filters:`** per **[Docs/CONFIG_MODEL.md](Docs/CONFIG_MODEL.md)** — try **`config/scenarios/layer_a_follow/`** for a wired example.

## Common commands

```bash
pip install -e ".[dev]"
ruff check src tests scripts
pytest tests/ -q
```

Opt-in network test: `set TYREX_NETWORK_TESTS=1 && pytest tests/test_resolution_network.py -v` (Windows).

## Documentation (start here)

| Doc | Purpose |
|-----|---------|
| **[Docs/README.md](Docs/README.md)** | Index and documentation map |
| **[Docs/Architecture.md](Docs/Architecture.md)** | System layout, diagrams, shadow vs live |
| **[Docs/developer_guide.md](Docs/developer_guide.md)** | Contributor boundaries and tests |
| **[Docs/CONFIG_MODEL.md](Docs/CONFIG_MODEL.md)** | YAML reference |
| **[Docs/OPERATIONS.md](Docs/OPERATIONS.md)** | Operator runbook |
| **[Docs/reporting_fact_model.md](Docs/reporting_fact_model.md)** | Structured reporting / joins |
| **[Docs/OPERATIONS.md](Docs/OPERATIONS.md)** § *Current status & operating model* | What is reliable today, limits, wallet model |
| **[Docs/Implementation/current_state.md](Docs/Implementation/current_state.md)** | Implementation status hub |
| **[Docs/Implementation/road_map.md](Docs/Implementation/road_map.md)** | **Archived** roadmap (historical) |
| **[Docs/modules/README.md](Docs/modules/README.md)** | Per-package **README** + **DEVELOPER.md** |

Dependency notes: **[Docs/dependency_lock.md](Docs/dependency_lock.md)** · Runbooks: **`Docs/Runbooks/`**.
