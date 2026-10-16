# BTC 5-minute market data course

Twelve executable lessons use the **same recorded 1 October 2026, 15:45–15:50 UTC
window**, from collection `2dd3d0d125dc4a9fb5e50daf3f9bfd3e`. The sample is selected
chronologically after fifteen minutes of lead-in, not on its returns. Missing data
and invalid books remain part of the lesson. No credentials or network are used by
the notebooks. No orders are placed, and the warehouse is never modified.

## Start here — Git Bash

From the repository root:

```bash
./.venv/Scripts/python.exe -m pip install -e '.[warehouse,research]'
./.venv/Scripts/python.exe -m jupyterlab src/tyrex_pm/research/tuto_btc5m
```

Use the project's Python environment as the notebook kernel. Open notebook 01,
choose **Restart Kernel and Run All**, then continue in numerical order. Every
notebook can also run independently. All code cells are implemented; exercises
are optional and include suggested answers below them.

Each lesson takes roughly 20–40 minutes to read and explore. The first book replay
in a kernel can take seconds rather than milliseconds: these are offline research
tools, not part of the trading decision loop. Subsequent calculations reuse it.

## Lessons

1. `01_meet_the_dataset.ipynb` — records, instruments, feeds, provenance and units.
2. `02_data_quality_and_time.ipynb` — coverage, reconnects, freshness and causal joins.
3. `03_one_market_lifecycle.ipynb` — Up/Down, bid/ask/mid, expiry and label availability.
4. `04_btc_short_horizon_statistics.ipynb` — returns, sampling, volatility and dependence.
5. `05_order_book_and_liquidity.ipynb` — depth, executable-size arithmetic and slippage.
6. `06_order_flow_and_book_dynamics.ipynb` — imbalance, OFI, depth changes and trade flow.

7. `07_expiry_and_market_behavior.ipynb` — countdown, phase comparisons and expiry uncertainty.
8. `08_cross_feed_alignment.ipynb` — receipt-time joins, reference TWAP, currency differences and lead/lag.
9. `09_probability_models.ipynb` — distance, remaining variance and digital-model sensitivity.
10. `10_trading_economics.ipynb` — $3 expected-value sizing, scenario costs, depth and delayed markouts.
11. `11_hypotheses_and_uncertainty.ipynb` — declared hypotheses, overlapping targets and dependent resampling.
12. `12_first_ml_dataset.ipynb` — causal features, nullable labels, purged splits and a baseline classifier.

The second half connects the observations to research decisions. Every notebook
has a question, input definitions, worked calculations, annotated figures, plain
language interpretation and exercises with suggested answers.

Lesson 09 distinguishes synthetic probability scenarios from recorded observations:
the sample has no attested official threshold or payout mapping. Its Binance anchor
is explicitly a proxy. Lesson 10 uses clearly labeled fee assumptions, not a
claimed current provider fee formula. A $3 expected value can still produce a
large realized loss; shares of one contract share one outcome.

Lesson 12 builds a real training pipeline, but predicts the **next five-second
Up midpoint rise**, not settlement or profit. One window permits a chronological
demonstration only. Optional additional prepared samples enable later-market
holdouts, with duplicate-market rejection and training-label/test-lookback purging.
Adjacent market windows can still be statistically dependent. Neither mode turns
these teaching scores into evidence of a deployable strategy.

## Recreate or change the sample

The default sample has already been prepared locally under `var/research/tuto_btc5m`.
The data is intentionally outside source control. On another checkout, recreate it:

```bash
./.venv/Scripts/python.exe -m tyrex_pm.research.tuto_btc5m.data \
  --root var/warehouse_phase_b_validation \
  --collection 2dd3d0d125dc4a9fb5e50daf3f9bfd3e \
  --output var/research/tuto_btc5m/my_sample
```

An existing output directory is rejected. `--market <market-id>` selects another
recorded BTC5m market explicitly. The preparation step filters normalized Arrow
batches, verifies each input part it reads, preserves original Decimal values and
nanosecond reception times, retains earlier outcome-book reconstruction history,
and pins output checksums in `manifest.json`. It does not rerun normalization,
quality publication or a costly full raw-archive verification.

Set `SAMPLE_PATH` in the setup cell of each notebook to your new sample directory.
Alternatively change `sample_settings.json` to its repository-relative path and
the **new manifest's SHA256** printed by preparation. The checked-in default hash
only applies to the original prepared sample. A rebuilt sample after compaction or
new metadata may have a different manifest even if the window looks the same.

This teaching snapshot is **not a qualified warehouse export**. Its integrity
scope covers selected normalized inputs and copied sample files, not every raw
message. Published quality is copied as a retrospective annotation. Unsupported
rules, payout mappings, thresholds and fees remain unsupported. Reference TWAP
observations are not silently used as an official opening strike or settlement.

## Validation of this course — 3 October 2026

All cells of lessons 01–06 executed successfully on the pinned recorded sample.
The final lesson was rerun after clarifying its snapshot policies. There are
**19 generated figures across the six lessons**, and **12 focused regression tests
pass**, covering causal joins, missing states, quiet-book seeds, nanosecond
precision, exact partial-depth arithmetic, socket/seed boundaries, snapshot-safe
flow, forward-label eligibility, and checksum tampering. Targeted Ruff and the
environment dependency check pass. No live recording or strategy test was run.

Executed copies with tables and charts are saved under
`var/research/tuto_btc5m/validation/`. The full execution receipt is `receipt.json`;
`receipt_06.json` records the final lesson-06 rerun. Source notebooks remain free
of saved outputs so new samples can be rerun cleanly.

One useful finding in this teaching window: the five-second diagnostic has no
same-seed comparisons, but has 122 observed comparisons when fresh usable snapshots
on the same socket are allowed. Lesson 06 explains why those are different
eligibility questions, and why neither establishes independent predictive evidence.

## Validation of lessons 07–12 — 5 October 2026

All cells of the six new notebooks executed successfully on the same pinned sample,
producing **22 figures**. Executed copies are in the validation directory above;
`receipt_07_12.json` records this run. The previous lessons were not re-executed.
Source notebooks 07–12 have no saved outputs.

**22 focused regression tests pass** across the original and new teaching helpers.
The added safeguards cover reference disconnects, probability symmetry, exact
scenario sizing, causal feature prefixes, missing labels, purged chronological
splits, duplicate markets, training-only preprocessing, single-class fallback and
resampling that respects missing time points. Targeted lint and dependency checks
also pass. This validation does not run live collection or qualify a strategy.

## Reproduce the validation

```bash
./.venv/Scripts/python.exe scripts/validate_btc5m_tutorials.py
./.venv/Scripts/python.exe scripts/validate_btc5m_tutorials.py --start-lesson 7
./.venv/Scripts/python.exe -m pytest tests/unit/research/test_tuto_btc5m.py tests/unit/research/test_tuto_btc5m_advanced.py -q
```

The validator checks notebook structure, executes every cell, and saves executed
copies and a receipt under `var/research/tuto_btc5m/validation`. It uses a temporary
kernel definition pointing at the current Python executable, rather than relying
on whichever global kernel happens to be installed. It needs local kernel sockets
but no provider connection. Source notebooks stay clean and reusable.

The first command executes all twelve lessons. The second limits execution to
07–12; `--lesson 12` runs just the ML lesson. A new full-course run overwrites
`receipt.json`; range and single-lesson runs use separate receipt names.

## Analytical conventions

- Feature time is local `received_at`, in UTC. Source time is shown separately.
- Joins use only observations available at or before the grid time.
- Validity, freshness and provider continuity assurance are different concepts.
- OFI increments reset across seed epochs. Forward prices can either require one
  seed or allow fresh usable snapshots on one continuous socket; the lesson declares
  that choice. No reconnect is treated as an ordinary order-flow event.
- Exact Decimal prices/quantities stay in the observations and depth accounting.
  Floats are explicit teaching views for plots and descriptive statistics.
- Regular-time plots do not become event-time estimates by accident. No return is
  inferred across a missing sample or observed Binance transport break.
- Displayed liquidity is not a promised fill; this course assumes no fee schedule,
  queue position, immediate complete-set merge, or guaranteed profit.
- One window can illustrate mechanics; it cannot establish a stable predictive
  edge, market-wide calibration or general distribution of BTC returns.

The notebooks link primary documentation and research where appropriate. Their
observations describe this recorded sample, not a validated trading strategy.
