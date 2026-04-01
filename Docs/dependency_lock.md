# Dependency lock (v1 development)

Captured from a **working dev environment**; v1.11 may formalize CI lockfiles.

| Package | Version (dev) | Notes |
|---------|----------------|--------|
| `nautilus_trader` | 1.222.0 | Install with `[polymarket]` extra (`pyproject.toml`) |
| `py_clob_client` | 0.34.1 | Polymarket CLOB |
| `PyYAML` | 6.0.3 | Allowlist + app config |
| `python-dotenv` | 1.1.0 | v1.00 auth script |

**Python:** 3.10+ (CI/dev uses 3.12 in one environment).

Regenerate after upgrades:

```bash
pip freeze | findstr /i "nautilus py_clob PyYAML python-dotenv"
```
