# settlement_value research notebooks

These notebooks run the **actual `settlement_value` plugin driver** on recorded warehouse windows through the closed-loop strategy replay (`research/strategy_replay`). They only configure experiments and draw results. Strategy logic lives in `strategies/settlement_value/`.

Plan and stage definitions: [`Docs/implementation/strategies/research_to_live_plan.md`](../../../../Docs/implementation/strategies/research_to_live_plan.md). Data interpretation: [`s0_data_interpretation.md`](../../../../Docs/implementation/strategies/s0_data_interpretation.md).

## Run

From the repository root, using the project environment (`pip install -e '.[warehouse,research]'`):

```bash
python -m jupyterlab src/tyrex_pm/research/strategy_lab
```

The notebooks read `var/warehouse_btc5m`, collection `380e44875c8e483aa4f76e17cab06c02`, by default (`experiments.Lab`). No network access is used and nothing is written to the warehouse. The current helpers may write reusable caches under `var/research/strategy_replay/cache`.

| Notebook | Stage | Runtime |
|---|---|---|
| `00_settlement_diagnostic.ipynb` | S0: candidate settlement rules against proxy outcomes | about 15 s |
| `01_settlement_value.ipynb` | M1: one window, real driver, `favorable` vs `friction` | about 1 min |
| `02_probability_quality.ipynb` | S2: model vs book probability quality, volatility sensitivity (reads `var/research/strategy_runs/s2/vol/`, pre-clock-correction) | about 1 min |
| `03_parameter_grid.ipynb` | S2: partial grid smoke result, side selection, skip reasons, and a read-only **results explorer** over `grid/summaries_partial.parquet` and `validation_seed/summaries.parquet` (kept separate; `explorer.py`; interactive part needs a running kernel and `ipywidgets`) | seconds |
| `04_staged_settlement_research.ipynb` | S2 staged workflow: fixed model/budget/execution baseline, small TWAP entry search (A, friction), Endpoint benchmark (B), 0–2 hand-picked finalists under favorable (C); results explored in place. Stages are disarmed by default (`RUN_STAGE_X = False`); orchestration in `staged.py`, results under `var/research/strategy_runs/staged/<experiment-id>/` | seconds to preview; stage A ≈ 35 min with 4 workers |

Corrected S2 numbers (clock correction, both backends) are in `var/research/strategy_runs/s2/validation_seed/` and summarised in `Docs/implementation/strategies/settlement_value_candidate_ranges.md`. Experiments are run with `python -m tyrex_pm.research.strategy_lab.s2_experiments {split,vol,grid,buffer,holdout}` (about 2.5 GB per worker; use 4–6 workers).

## Beginner reading path

Read **00, then 01**, running cells in order with the project Python kernel.
Both notebooks now include definitions, worked examples, output-reading guides
and short exercises with suggested answers:

- [00: settlement diagnostic](00_settlement_diagnostic.ipynb) explains outcome
  tokens, the opening threshold, time-weighted averages, data origins, source
  versus receipt timestamps, candidate-rule agreement and the two histograms.
  Allow about 25–35 minutes for reading and exploration.
- [01: settlement value](01_settlement_value.ipynb) explains the strategy's
  probability inputs, the endpoint model, uncertainty bands, fees, expected
  profit, parameters, execution assumptions, every chart panel and the order
  ledger. Allow about 40–55 minutes for reading and exploration.

The numerical examples are explicitly synthetic. Actual experiment cells still
call the real plugin and replay; the notebooks do not duplicate strategy logic.
Inspect the expanded configuration and scenario table rather than treating a
tutorial value as a recommended trading parameter.

## Probability-vs-book timelines (explorer section 6)

`ex.Explorer` plots the selected run's Up and Down model probabilities (point and conservative) against each book's own midpoint, with ENTER requests and any recorded fills. `timelines.py` looks for a trace in this order:

1. **The run's own bundle.** `decisions.parquet` next to the summaries: `s2/validation_seed` (all 180 runs), or a staged stage run with `keep_timelines=True`.
2. **A cached diagnostic replay** in `var/research/strategy_replay/timelines/`.
3. **Nothing.** The explorer then offers **Generate timeline for this selected run**.

That button replays one window × configuration × scenario after checking that the effective configuration hash, the recorded data and the scenario still match. It then compares decision digests. A different digest is labelled a *new diagnostic replay*, and its fills are not shown as the run's execution. Summaries and P&L are never modified.

Future batches also save each run's order and fill events in the `execution_events` summary column.

## Labels on every result

- **K** is sealed from the exact-start TWAP tick without attestation, so its quality is `PROVISIONAL`. Research configs accept it explicitly (`experiments.RESEARCH_BASE_PARAMETERS`).
- **Outcome** is `label_type: proxy_outcome`: the single positive entry of the recorded payout vector, with the payout unit `UNKNOWN`. It is not a qualified settlement label.
- **Reference series** is the warehouse PolyBolt TWAP feed, not the live RTDS stream.
- **Execution** follows `config/research/scenarios/{favorable,friction}.yaml`. Both are explicit and uncalibrated; `favorable` is not a profitability bound.
- **Fees** use the scenario curve (r = 0.07, e = 1, taker, charged in shares).

A single window, or the current ~58-window collection, supports diagnostics and candidate ranges only. It does not validate profitability.

## Programmatic use

```python
from tyrex_pm.research.strategy_lab.experiments import Lab, grid
lab = Lab()
result = lab.run(lab.windows()[0].market_id, parameters={"entry": {"min_edge_per_share": "0.03"}},
                 scenario="friction", save=True)   # -> var/research/strategy_runs/<collection>/
```

`result["decision_digest"]` identifies a run by its decisions, fills and economics. Wall-clock time, durations and file paths are excluded. Identical inputs and configuration reproduce the same digest.

## Speed

`Lab` caches the catalog and prepared windows on disk, as well as loaded windows
in memory. A first load can take longer than a rerun; replay time depends on the
data, configuration and current engine. Set `Lab(cache_dir=None)` to disable the
disk cache. Cache identity includes input and loader versions; it is a performance
aid, not evidence that a dataset or strategy is qualified.

Notebook 01 explicitly selects `endpoint_diffusion` and displays all effective
settings. Other parameters still inherit current defaults, so compare the expanded
configuration when a rerun differs from an older saved result.
