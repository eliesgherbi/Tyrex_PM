# Notebook 06 on the production path (integration plan P6)

Status: implemented 2026-10-19.

## What changed

The notebook 06 hypotheses are strategy configuration. They are not study hooks.

| Variant | Parameter overlay |
|---|---|
| unchanged | `{}` (`variance_time_policy: per_sample_v0`, the original formula) |
| time_corrected | `volatility.variance_time_policy: elapsed_variance_v1` |
| time_corrected_gap_veto | the above plus `disagreement_veto: {enabled: true, max_gap_pp: 9}` |

- `research/strategy_lab/case_runs.py`:
  - `case_specs` / `run_case_set` build case set × candidates × variants as `batch.run_batch` specs. Each spec carries the execution contract and the `recording:` block (`explain` by default), so `explanations.parquet` holds the indicator state.
  - `case_run_directory` gives an identity-keyed folder under `var/research/case_runs/` that can be resumed.
  - `latest_case_run` finds the most recent run.
  - `case_frames` loads a case-run folder, or a saved notebook 06 study bundle (the legacy adapter, read only), into one layout. The layout has summaries, decisions and explanations, with `window_id`, `candidate`, `variant`, `decision_class` and `vetoed_evaluations` added.
- The batch decision columns gain `tag`, `disagreement_gap_pp` and `disagreement_reason`.
- Notebook 06 now uses `case_runs`. Its volatility probe uses `variance_time_policy`. It reads the latest case run, or the saved study bundle until one exists.
- Removed after the migration check:
  - the `research_study` hook in `run_window` and `ReplayAgent`;
  - from `extreme_cases`: `StudyIndicatorGraph`, `ProbabilityStudy`, `gap_reason`, `variants`, `replay_cases`, `saved_results`, `latest_case_run`;
  - their tests. Their coverage now lives in `test_ewma_variance_time_policy.py` (policy kept across a reconnect) and `test_disagreement_veto.py`.

## Migration check (amendments 1 and 2)

`case_runs.migration_check` re-runs every saved study bundle on the production path and compares normalized behaviour:

- evaluation instants;
- per-evaluation decision class, where a study veto and a strategy veto are both `VETO`;
- entries (time, side);
- simulated fills (time, instrument, quantity, price, fee);
- economics.

Identity metadata is not compared. Reports are in `var/research/case_runs/migration_20261019/`.

Source: `development_cases_v1-51563af1bc8c`, 7 cases × 2 candidates × 3 variants = 42 runs, friction scenario, `reference_availability_v1`.

| Contract | Trading equal (entries, fills, economics) | Every decision class equal |
|---|---|---|
| `replay_execution_contract_v1` | 42 / 42 | 28 / 42 |
| `replay_execution_contract_v2` | 42 / 42 | not used for acceptance (v2 is measured separately) |

### The 14 class differences are the 14 veto runs

The study vetoed every evaluation whose gap exceeded 9 pp, before any strategy check. That covered evaluations outside the entry band, after the window closed, and those with no edge, and each was recorded as `BLOCKED / ENTRY_NOT_ALLOWED`. The strategy veto applies only where an entry could otherwise be made.

The same evaluations now carry the strategy's real reason. Totals over the 14 runs:

| Reason now | Evaluations |
|---|---|
| `OUTSIDE_ENTRY_BAND` | 667 |
| `ENTRY_WINDOW_CLOSED` | 649 |
| `EDGE_BELOW_COST` | 324 |
| `BOOK_NOT_READY` | 264 |
| `DEPTH_INSUFFICIENT` | 206 |
| `DISAGREEMENT_VETO` (true vetoes) | 204 |
| `MODEL_NOT_READY` | 48 |
| `PENDING_ORDER → EPISODE_CONSUMED` (post-entry label in one run) | 128 |

Entries, fills and economics are identical, so these are reason-label changes of the kind amendment 1 expects. They are not behaviour changes.

Contract v2 (the live "indicators ready" gate) changed no entry, fill or economics on these cases.
