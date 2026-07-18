# Development

**Purpose:** how to work on the active package safely.

## Source layout

Active code: `src/tyrex_pm/`.  
Tests: `tests/` (pytest `testpaths`).  
Legacy archive: `old/` — **never import**.  
Docs: `Docs/latest` (current), `Docs/specifications`, `Docs/implementation`.

## Install

```bash
pip install -e ".[dev]"
# authenticated client libs when needed:
pip install -e ".[dev,live]"
```

Python ≥ 3.11.

## Formatting / linting

Configured in `pyproject.toml`:

- **Ruff** — `line-length = 100`, `target-version = py311`, lint select `E,F,I`
- **Coverage** tools configured; omit `old/*`

Run what you have installed, e.g. `ruff check src tests` if ruff is available in your environment. Pytest is the required gate.

## Tests

```bash
pytest
```

Conventions:

- Fixtures under `tests/fixtures/`
- Default tests offline — no live CLOB hosts in unit tests
- Architecture/import firewalls must stay green

## Documentation

- Change behavior → update `Docs/latest/`
- Phase evidence → `Docs/implementation/`
- Objectives/baselines → `Docs/specifications/`
- Style rules: [documentation_style.md](documentation_style.md)

## Commit hygiene

- Do not commit `.env`, credentials, or `var/`
- Do not import or revive `old/` into `src/`
- Prefer small, phase-scoped commits
- Dirty worktrees block `--execute-live`

## Dirty worktrees

For venue-mutation paths the runtime requires a clean tree. Documentation, offline, and dry (no venue mutation) work may proceed dirty; do not bypass live cleanliness.
