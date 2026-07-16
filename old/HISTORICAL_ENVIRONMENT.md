# Historical environment record (pre-R1 archive)

Recorded during R1 archive on branch `rest_project`. Contains no secret values.

| Field | Value |
|-------|--------|
| Branch | `rest_project` |
| Pre-reset commit SHA | `35f83fd986dfe91aa952787644d895bdeb1f75e4` |
| Pre-reset commit subject | `starting reflexion on the reset` |
| Python version (recording host) | 3.12.3 |
| Historical package name | `tyrex-pm` (`pyproject.toml` name) |
| Historical import package | `tyrex_pm` under former `src/tyrex_pm/` (now `old/src/tyrex_pm/`) |
| Historical CLI entry | `tyrex-pm = tyrex_pm.runtime.app:main` |
| Historical module invocation | `python -m tyrex_pm.runtime.app` |
| Historical requires-python | `>=3.11` |
| Historical core deps | `httpx>=0.27.0,<1`, `pyyaml>=6.0.1` |
| Historical extras | `dev` (pytest, pytest-asyncio), `live` (py-clob-client-v2, websockets, …), `record`, `research` |
| Lockfile | None present at archive time (no `uv.lock` / `poetry.lock` / `requirements.txt` at root) |
| Historical test command | `pytest` with `testpaths = ["tests"]` (former root `tests/`, now `old/tests/`) |
| Historical config locations | `config/risk/`, `config/runtime/`, `config/strategies/`, `config/scenarios/` → now under `old/config/` |
| Historical documentation | Root `Docs/` → now `old/Docs/` (includes `Implementation/z_gap_architecture_reset/`) |
| Historical research | `research/` → `old/research/` |
| Historical scripts | `scripts/` → `old/scripts/` |
| Useful runtime evidence (not archived) | `var/reporting/runs/`, `var/reporting/z_gap/`, `var/state/`, `var/recordings/` (~11.5 GB total under `var/`) |
| `var/` archived? | **No** — left at repository root, gitignored |
| `.env` copied or modified? | **No** — remained at repository root; checksum verified before/after R1 |
| `.env.example` | Moved to `old/.env.example`; new sanitized root template created for the reset |

## Archive layout

```text
old/
  src/
  tests/
  scripts/
  config/
  Docs/
  research/
  ops/
  pyproject.toml
  README.md
  commandes.md
  .env.example
  HISTORICAL_ENVIRONMENT.md   # this file
```

## Isolation note

The archived tree is reference-only. It must not be installed, imported, or collected by the active project's packaging or tests.
