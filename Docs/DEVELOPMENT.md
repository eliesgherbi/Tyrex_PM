# Development quick start

**Full contributor guide:** [developer_guide.md](developer_guide.md) · **Architecture:** [Architecture.md](Architecture.md) · **Doc index:** [README.md](README.md)

## Setup

```bash
pip install -e ".[dev]"
pytest tests/ -q
ruff check src tests scripts
```

## What to read first

1. [Architecture.md](Architecture.md) — data flow and module boundaries  
2. [developer_guide.md](developer_guide.md) — where to add behavior, anti-patterns  
3. [CONFIG_MODEL.md](CONFIG_MODEL.md) — YAML surfaces  
4. [modules/README.md](modules/README.md) — links to package-level guides  
5. [OPERATIONS.md](OPERATIONS.md) § *Current status & operating model* — framework vs wallet truth, reconciliation limits, validation links (read before changing live risk or ingest)  

## Module deep dives

Mature packages have **`DEVELOPER.md`** next to **`README.md`** under `Docs/modules/<package>/` (risk, execution, runtime, reporting, data, signal, strategy).
