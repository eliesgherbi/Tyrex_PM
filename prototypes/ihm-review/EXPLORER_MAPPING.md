# Explorer → IHM review mapping

This is an isolated frontend prototype for review, not Step 3/4 backend integration. All market and execution data are synthetic. Do not use these fixtures as trading, paper-account or reconstruction evidence.

## What to review

1. Open Research → Explorer. The scope is always visible: dataset, strategy adapter, execution scenario and configuration.
2. Review known simulated P&L, filled/requested windows, resolved-fill win rate and acquisition spending. Open a metric to see its denominator.
3. Inspect expected profit at entry separately from realized P&L. The fixture assumes 10 intended shares and a projected collateral fee. These are illustrative calculations; integration must consume the explorer's recorded EV fields.
4. Select a window in the bar chart or window selector. The Up and Down panels, window details, reason counts and event list follow it.
5. Click either timeline or an event. The instant, model values, independent books and recorded-input inspector follow that timestamp.
6. Toggle warm-up, strict gaps, ask, point and conservative curves. Zoom or drag the time-range slider.
7. Switch trace provenance. The diagnostic-view demonstration removes original fill/arrival/sale markers.
8. Switch to ask70. It reuses the layout with asks, own-book midpoints and threshold. It supplies no point or conservative model. This sketch does not add ask70 replay-lab support.
9. Use Compare, Experiments and Candidates to review the remaining journey. Job progress is a browser animation; it does not compute a new saved bundle.
10. Open Runs and Evidence. Paper launch remains disabled and no current account exposure is invented.

## Source meanings to preserve

- `strategy_lab/explorer.py:summarize, summary_table`: scope, requests, fills, resolved/unresolved fills, P&L, win-rate denominator, spending and entry estimates.
- `explorer.py:plot_windows, plot_distribution, window_detail`: per-window outcomes, missingness, distributions and detail fields.
- `timelines.py:curves`: Up point = p_up_point; Up conservative = p_up_low; Down point = 1 - p_up_point; Down conservative = 1 - p_up_high. Down conservative is NOT 1 - p_up_low.
- Each midpoint is (bid + ask)/2 from that leg's own book. A token price in USDC/share is not a verified probability. The prototype uses separate left probability and right price axes sharing the 0–1 range.
- `timelines.py:markers, plot_timeline`: entry requests use conservative model probability; fills and sales use execution prices. Arrival is a timestamp without a fabricated price coordinate.
- `timelines.py:line_series`: missing evaluations remain gaps. Strict display breaks every missing evaluation; relaxed display may join only within the declared limit.
- `timelines.py:explanation_table, reason_counts`: recorded inputs, units, causal timing and reason populations.
- Original saved traces and regenerated diagnostics remain distinct; missing evidence is not reconstructed merely by opening a screen.
- `strategies/ask70/strategy.py:on_decision`: own-book asks and threshold evidence; no settlement model series.

## Proposed reusable view adapter

Common fields: schema/version, strategy kind, configuration/scenario/data identity, trace provenance, time bounds, own-book quotes, decisions/reasons, execution-origin events, view availability and limitations.

Optional per-strategy series declare name, meaning, units, leg, source field and availability. Probability complements and conservative-bound mappings belong to the strategy adapter. Do not generalize settlement's formulas to every strategy. Missing series are omitted with an explanation.

Keep view projection in the Python workflow/read-adapter layer after integration. React renders returned series and metadata; it does not recalculate strategy decisions, indicators or fees. Missing traces report availability. Diagnostic generation, evaluation access, candidate freeze and paper launch remain explicit operations with their existing guards.

## Still required after visual approval

- Agree which explorer fields and views are required and retain an explicit parity checklist.
- Add a thin, validated local API over the Step 3 operation catalog.
- Supply bounded/downsampled views without losing source identity, events or gaps.
- Bind controls to real operation outputs; remove synthetic state and simulations.
- Test fresh-browser selection/job/result/candidate equivalence and screen errors.
- Accept the applicable Step 2/P4/P5/P6 evidence before enabling gated controls.

