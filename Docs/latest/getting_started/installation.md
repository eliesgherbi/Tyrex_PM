# Installation

**Purpose:** install the active package for local development and verification.  
**Checkpoint:** `fb9d0d8` · package `tyrex-pm` `0.3.0` · `requires-python >=3.11`

## Requirements

- Python **3.11+** (see `pyproject.toml`)
- Repository root as the working directory for all commands below
- Recommended: a virtual environment (venv / conda)

Optional extras:

| Extra | Use |
|-------|-----|
| `[dev]` | `pytest` |
| `[live]` | `py-clob-client-v2`, `eth-account` (authenticated / mutation-capable tooling) |

Public observe/shadow fixture paths do not require credentials. Never commit `.env`.

## Editable install

From the repository root:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Unix:    source .venv/bin/activate
pip install -e ".[dev]"
```

For authenticated read / live client libraries:

```bash
pip install -e ".[dev,live]"
```

## Verify CLI

```bash
tyrex-pm version
# or
python -m tyrex_pm.application.cli version
```

Expected: prints `0.3.0` (or the version in `pyproject.toml`).

```bash
tyrex-pm --help
```

## Run tests

```bash
pytest
```

Default tests are offline (fixtures / fakes). They must not import `old/`.

## Secrets

Copy [`.env.example`](../../../.env.example) to `.env` only when you need authenticated paths.  
Do not paste real keys into docs, commits, or chat logs.

## Not provided

Docker images, deployment playbooks, and PyPI release instructions are **out of scope** for the current maturity.
