# V2 hold-to-resolution baseline: completed A/B/C and provisional default

Experiment: `full_development-7f132d3ee2`. Replay: `strategy_closed_loop_v2`, original share-based execution.
Scope: settlement_value, one entry attempt per window, hold to resolution, no retries,
early exits, re-entry or new live-execution features. The experiment source stayed unchanged.

## Decision

Freeze **TWAP, minimum conservative edge 2 cents/share, entry from 150 seconds remaining
until 30 seconds remaining** as the provisional development baseline. This implements the
user's preference for less concentrated performance and completes the workflow; it is
not a claim that this is the optimal or live-ready strategy. Detailed KPI selection is deferred.

Keep **TWAP 5 cents / 240 seconds** as the raw-P&L comparison candidate.

Full effective parameters: [baseline JSON](../../../config/research/settlement_value_hold_baseline_v2.json).
Selection/provenance: [selection manifest](../../../var/research/strategy_runs/staged/full_development-7f132d3ee2/baseline_review/baseline_selection.json).

## What A, B and C mean

- **A:** six TWAP settings under friction, each on the same 94 development windows:
  564 runs. Only edge (2/5/8 cents) and earliest entry time (240/150 seconds remaining)
  vary. This is a small entry search with other parameters fixed.
- **B:** one Endpoint benchmark at 5 cents / 240 seconds under friction on those same
  94 windows: 94 runs. Compare it with A's TWAP 5-cent/240-second row to isolate the
  model change. B is not another split, a new data period or a full Endpoint grid.
- **C:** the two preselected A finalists under favorable execution on the same windows:
  188 runs. It changes latency, depth participation and adverse ticks; it does not
  change the entry parameters or provide untouched-data validation. Favorable is not
  a guaranteed profitability upper bound.

All 846 simulations are complete, with timelines. No holdout strategy evaluation was
launched or used for selection.

## Results used for the provisional choice

- Selected 2-cent/150-second baseline: friction $62.32,
  36 fills from 45 requests,
  29 wins / 7 losses; favorable
  $74.72, 44 fills.
- Raw leader 5-cent/240-second candidate: friction $80.09,
  34 fills; favorable $97.58,
  41 fills.
- Endpoint benchmark: friction $44.00, 63 fills,
  31 wins / 32 losses.
- Selected baseline's friction P&L by recording date:
  2026-10-01: $-4.86, 2026-10-06: $30.41, 2026-10-15: $36.77.
- The raw leader's largest win contributes $46.50 of its
  $80.09. Removing each candidate's largest win leaves
  $39.89 for the selected baseline and
  $33.59 for the raw leader. This is a diagnostic,
  not a reason to discard legitimate winning trades.

The preference is based on distribution across recordings and lower concentration,
not merely the highest win rate. Favorable/friction outcomes can differ in which
signal is reached or filled, so favorable need not always make more P&L.

## Fixed parameters and how to reuse them

- Model: `twap_proxy_diffusion`; sigma band 0.25, reference band 1 bps, Binance spot.
- Volatility: half-life 30 seconds, sigma floor 0.00001, sigma scale 1.0.
- Entry: 2-cent minimum edge, remaining-time band 30–150 seconds,
  maximum price 0.97, one-tick limit buffer, `cap_only` sizing.
- Intended debit/loss target: 5 USDC; actual v2 replay debit can exceed this target.
- The $3 expected-profit field stays a research diagnostic, not a forced sizing target.

In notebook 04 the frozen JSON is loaded as `BASELINE_PARAMETERS`. Its six-point grid
remains unchanged for reproducibility. Reopening/running the notebook leaves all stages
disarmed. B/C are already complete, so no rerun is needed to view them.

For future v3 experiments, explicitly select the live-aligned order contract and start
with retry off, hold-to-resolution and one trade per window. First measure alignment
alone, then compare retry and exit variants against that baseline. The v2 identity and
economics are not interchangeable with v3; this file freezes the strategy hypothesis,
not a production execution profile.

## Limits and deferred work

- All outcomes are proxy-labelled; K is provisional, fees and execution are assumed.
- 94 windows come from only three recording dates; nearby windows are dependent.
- These totals sum independent window replays; they do not test continuous shared
  capital or redemption availability. That is a later ordered-window experiment.
- All A filled runs carry `gap_with_exposure` and `order_constraints_unqualified` flags.
  The biggest win's book becomes invalid after entry at several in-window timestamps;
  do not interpret the traces as uninterrupted executable liquidity for exit research.
- V2's share-based friction fills can exceed the intended debit target. Selected
  baseline maximum actual debit is $5.29, with
  24 runs above 5 USDC; raw leader maximum is
  $5.55. This study preserves v2, rather than silently
  changing execution rules mid-experiment.
- Window-bootstrap intervals in the analysis assume independence and do not account
  for grid selection or serial dependence. They do not establish validated profitability.
- Detailed risk/KPI selection, untouched-data evaluation, provider qualification and
  live activation are outside this workflow closeout. No real orders were submitted.

## Reproducibility

Source fingerprint: `f60ef5853cfd3a60ff4894fb5b7bca45f773632529707b6f23ad8cec7f92c21f`. Result files are under
`var/research/strategy_runs/staged/full_development-7f132d3ee2/stage_{A,B,C}/`.
Scripts `finish_settlement_baseline_2026_10_16.py` and
`review_settlement_baseline_2026_10_16.py` preserve stage identities and read saved outputs.
The original notebook is backed up under `scratchpad/baseline_v2_closeout_2026-10-16/`.
