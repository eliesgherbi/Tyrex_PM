# M2B.7 — Survival case labeler

## 1. Purpose in simple terms

Automatically label survival episodes from facts + recorded market path + resolution into a `survival_cases` Parquet table. Rule-based only — no subjective labels. Powers notebooks 03–06 and informs Phase 3 calibration.

## 2. Boundary

### In scope

- `research/labeler/` rule engine
- Case types per plan §14: `clean_recovery_trailing_exit`, `quality_gap_recovered`, `liquidity_vacuum_after_trigger`, `profit_lock_missed_continuation`, `failed_before_recovery`, `strong_survivor_preclose_flatten`, `execution_abandoned`, `never_armed_and_lost`
- `survival_cases` table in data lake
- Notebooks 03–06
- ≥500 labeled episodes acceptance

### Out of scope

- Acting on labels in live trading
- ML training pipelines
- Changing survival enforce behavior

## 3. Current repo reality

| Path | State |
|------|-------|
| `survival/facts.py` | Rich survival fact payloads |
| M2B.3 | `strategy_events` table from facts |
| M2B.5 | Synthetic episodes multiply sample size |

## 4. Files to create

| File | Why |
|------|-----|
| `research/labeler/rules.py` | Case classification rules |
| `research/labeler/run.py` | Batch labeler CLI |
| `research/notebooks/03_*.ipynb` … `06_*.ipynb` | Analysis + memos |
| `tests/test_survival_labeler_rules.py` | Golden labeled episodes |

## 5. Files allowed to modify

`research/normalize/tables/survival_cases.py` — table writer.

## 6. Forbidden files / modules

```text
survival/enforcement.py, survival/enforcement_dispatch.py
paired_binary_run.py
```

## 7. Interfaces and contracts

```python
def label_episode(
    facts: list[dict],
    market_path: pd.DataFrame,
    resolution: dict,
) -> list[SurvivalCaseLabel]: ...
```

Output schema per plan §10 `survival_cases` columns.

## 8. Runtime flags and rollback

Offline batch only.

## 9. Tests required

Golden episodes with expected case labels; notebook 06 go/no-go memo exists.

## 10. Regression tests required

Labeler does not import live OMS/risk.

## 11. Acceptance criteria

- [ ] ≥500 labeled episodes across historical + synthetic
- [ ] Notebook 06 runner go/no-go memo written
- [ ] Rules documented in labeler module docstring

## 12. Divergence risks

| Risk | Control |
|------|---------|
| Subjective labels | Rule-only spec |
| Labels feeding live enforce | Out of scope until Phase 3 |

## 13. Review checklist

- [ ] Each case type has explicit rule predicate
- [ ] `never_armed_and_lost` bucket counted

## 14. Done / not done examples

**Done:** `survival_cases.parquet` with 500+ rows.  
**Not done:** ContinuationRunnerPolicy (Phase 3).

## 15. Next milestone dependency

**M2B.8** may use label statistics for advisory table evidence only.
