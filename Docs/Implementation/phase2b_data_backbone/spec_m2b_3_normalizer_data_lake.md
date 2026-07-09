# M2B.3 — Normalizer + Parquet data lake

## 1. Purpose in simple terms

Batch-convert recorded JSONL(.zst) archives into **analysis-ready Parquet tables** with per-market quality scorecards. Raw JSONL remains the archive; tables are regenerable.

## 2. Boundary

### In scope

- `research/normalize/` batch job
- Tables per plan: `markets`, `book_deltas`, `book_snapshots`, `trades`, `ws_quality`, `btc_ticks`, `strategy_events` (facts join), `resolutions`
- `Docs/DATA_LAKE.md`
- `pandas` + `pyarrow` as optional `[research]` extra
- `tests/test_normalize_golden.py`
- `tests/test_research_import_isolation.py` (live must not import research)

### Out of scope

- Feature computation (M2B.6)
- Survival labels (M2B.7)
- Live runtime changes
- Modifying recorded JSONL in place

## 3. Current repo reality

| Path | State |
|------|-------|
| `research/` | **Does not exist** |
| `pyproject.toml` | No pandas/pyarrow |
| `scripts/replay_survival_advisory.py` | Facts-only replay — not event normalizer |
| M2B.1-B | JSONL.zst + manifests exist |

## 4. Files to create

| File | Why |
|------|-----|
| `research/__init__.py` | Package root |
| `research/normalize/__init__.py` | Normalizer |
| `research/normalize/run.py` | CLI entry |
| `research/normalize/tables/*.py` | Per-table builders |
| `Docs/DATA_LAKE.md` | Schema registry |
| `tests/test_normalize_golden.py` | Golden fixture day |
| `tests/test_research_import_isolation.py` | Import boundary |

## 5. Files allowed to modify

| File | Modification |
|------|--------------|
| `pyproject.toml` | `[research]` optional deps |
| `Docs/README.md` | Link to DATA_LAKE |

## 6. Forbidden files / modules

```text
src/tyrex_pm/** must not import research.*
risk/*, execution/*, paired_binary_run.py
```

## 7. Interfaces and contracts

### CLI

```bash
python -m research.normalize.run --recordings var/recordings/2026-07-03 --out var/parquet/
```

### Partition layout

```text
var/parquet/date=YYYY-MM-DD/market_id=<id>/<table>.parquet
```

### Quality scorecard fields

`coverage_pct`, gap count, max staleness, duplicate rate — joined into `markets` table.

## 8. Runtime flags and rollback

Offline batch only — no runtime flag. Rollback: do not run normalizer.

## 9. Tests required

Golden normalization of committed trimmed fixture; corrupted fixture raises scorecard flags.

## 10. Regression tests required

All live `src/` tests green; import isolation test passes.

## 11. Acceptance criteria

- [ ] Full recorded day normalizes end-to-end
- [ ] `markets.coverage_pct` populated
- [ ] `DATA_LAKE.md` documents all columns
- [ ] `test_research_import_isolation.py` passes

## 12. Divergence risks

| Risk | Control |
|------|---------|
| Live imports research | CI isolation test |
| Adding pandas to core deps | Optional extra only |

## 13. Review checklist

- [ ] research/ never imported from src/
- [ ] Golden test committed
- [ ] Scorecard flags synthetic gaps

## 14. Done / not done examples

**Done:** `book_deltas.parquet` row count matches JSONL book events.  
**Not done:** Notebook analysis (M2B.4).

## 15. Next milestone dependency

**M2B.4**, **M2B.5**, **M2B.7** consume Parquet outputs.
