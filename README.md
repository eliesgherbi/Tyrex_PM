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

`run_guru` also writes **`logs/<mode>/run_tyrex.log`** (Tyrex `tyrex_pm.*` loggers) and **`run_nautilus.log`** (Nautilus framework file sink); `--log-name` applies to both stems. See `Docs/OPERATIONS.md`, **`Docs/logging_system_guide.md`**, and **`Docs/log_validation_playbook.md`**.

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

- **Current state / migration hub:** [Docs/Implementation/current_state.md](Docs/Implementation/current_state.md) — what is **complete / partial / blocked**, Nautilus vs Tyrex ownership.
- **Roadmap (strategic):** [Docs/Implementation/road_map.md](Docs/Implementation/road_map.md) — Phase A–C intent + **implementation snapshot** cross-reference.
- **Phase A closure:** [Docs/Implementation/phase_a_closure.md](Docs/Implementation/phase_a_closure.md) — pending leaves, positions, capital gate, restart.
- **Phase B plan:** [Docs/Implementation/Phase_B_planing.md](Docs/Implementation/Phase_B_planing.md) — B0–B5 **implemented**; operator matrix in [Docs/OPERATIONS.md](Docs/OPERATIONS.md) § Phase B.
- **Phase B operational validation (pre–Phase C):** [Docs/Implementation/phase_b_operational_validation.md](Docs/Implementation/phase_b_operational_validation.md) — restarts, marks, denial semantics, live checklist.
- **Phase A+B test vs live matrix:** [Docs/Implementation/phase_ab_test_validation_matrix.md](Docs/Implementation/phase_ab_test_validation_matrix.md) — what pytest proves, what docs cover, what needs `run_guru` / live.
- **Architecture:** [Docs/Architecture.md](Docs/Architecture.md) — system layout, diagrams, shadow vs live vs framework path.
- **Per-module:** [Docs/modules/README.md](Docs/modules/README.md).
- **Developers:** [Docs/DEVELOPMENT.md](Docs/DEVELOPMENT.md) — tests, conventions, compose.
- **Config fields:** [Docs/CONFIG_MODEL.md](Docs/CONFIG_MODEL.md).
- **Operators:** [Docs/OPERATIONS.md](Docs/OPERATIONS.md).
- **Runbooks:** `Docs/Runbooks/`.
- **Doc reconciliation log:** [Docs/Implementation/documentation_reconciliation_2026-04.md](Docs/Implementation/documentation_reconciliation_2026-04.md).
