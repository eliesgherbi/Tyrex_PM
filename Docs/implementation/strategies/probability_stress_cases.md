# Probability stress-case workflow (notebook 06)

## Objective and boundaries

Keep the two frozen hold configurations fixed while diagnosing extreme probability
disagreement. Do not rerun a grid or optimize a threshold against the two live losses.
Notebook 06 is a development/regression bench. It is not holdout qualification.

The seven replayable cases span all three development recordings and include
ordinary winners, ordinary losses, a large-gap winner and a large-gap observation
whose eventual direction was correct. Four compact live-evidence cases include the
two large-gap losses and smaller-gap controls. Old live cases cannot be replayed
exactly without their raw input history; the notebook labels this limitation.

## Order of work

1. Review the frozen inventory and probability paths, especially at the actual entry.
2. Test the units/time invariants of `elapsed_variance_v1`.
3. Rerun both fixed configurations on the seven recorded cases under v3 execution,
   friction and `reference_availability_v1`: unchanged, corrected time, corrected
   time plus the exploratory 9-percentage-point veto. This is 42 runs, not a grid.
4. Check unchanged trade results against saved notebook 05 results before assessing
   effects of the alternatives. Inspect each avoided loss and each lost/delayed win.
5. Freeze a hypothesis and evaluate broader development windows, then untouched data.
6. Integrate optional full live traces and collect raw causal inputs during observation
   before claiming a live case is explained by the estimator correction.

## Implementation status

- The default EWMA remains `legacy`. `SigmaConfig.time_policy=elapsed_variance_v1`
  is available to the explicit research overlay, not added to live run templates.
- The alternative estimates variance per second using r²/dt and an elapsed-time
  half-life; zero observed returns decay established variance and jump thresholds
  scale with sqrt(dt). Missing prices are not filled forward as synthetic samples.
- `ProbabilityStudy` optionally adds a causal pre-entry disagreement veto. It sets
  entry permission before the real strategy evaluates, without consuming an attempt.
  Held/pending positions keep their normal lifecycle. The entry gate is research-only.
- Optional `summary`, `decisions`, and `full` research output levels are implemented.
  Full includes feature/reference snapshots and volatility calculation state. This
  controls replay output, **not live recording**; full live tracing remains outstanding.
- Output identities include code, helper, exact case list, parameters, scenario,
  execution policy and source/cache fingerprints. Results are written separately
  under `var/research/settlement_extreme_cases/`; ordinary replay has no study hook.

## Interpretation rules

A point gap is useful competing evidence, not proof of model error. A window-average
gap is retrospective; it cannot be an entry filter. A point threshold can remove
valuable alpha. Test it causally against fresh, non-crossed, two-sided quotes, alongside
existing readiness and price/edge/risk checks. A stronger later hypothesis could
combine persistent disagreement beyond the sensitivity band with input-quality
requirements; it has not been implemented or selected here.

An entry-time veto must remain a wait, not close the window or start a retry episode.
When it clears, re-evaluate every normal condition. Retry cannot guarantee a later
entry and cannot repair a position already bought. Existing hold positions stay held.

Research outcomes still use proxy labels. No selected-case result qualifies real-money
use, and the short encouraging channel comparison does not prove feed reliability.

## Evidence reuse during construction

The research veto initially evaluated time remaining from raw replay time rather than
the strategy's corrected snapshot time. It now uses the exact same timestamp as the
strategy, with a regression test deliberately offsetting those clocks. The original
partial run was preserved. A separate `reuse_proof.json` in the completed case directory
records the exact helper-only diff and checks every applicable gate outcome against
the recorded strategy probability. Only decision-equivalent bundles were reused;
other runs use the corrected implementation. This is not permission to mix arbitrary
code changes into an existing experiment.

## Completed first case review

All 42 runs are saved in `var/research/settlement_extreme_cases/development_cases_v1-51563af1bc8c/`.
The 14 unchanged candidate/window pairs reproduce the earlier fills, sides, debit and
P&L. Probability reconstruction and bounds checks passed for 13,536 saved probability
rows. The notebook has been executed with replay launching disabled and shows these outputs.

- Default, seven selected cases: unchanged proxy P&L −5.1937; corrected time +5.0541;
  corrected time plus 9-point veto +0.9289 USDC. The veto removed three corrected-path
  profitable cases and avoided one corrected-path loss.
- Comparison, same cases: unchanged +25.4276; corrected time +12.6796; corrected time
  plus veto 0 USDC, with no fills. It removed five corrected-path profitable cases
  and avoided one corrected-path loss.

These deliberately selected cases do not establish overall performance. The 9-point
veto is too restrictive on this set to recommend enabling. The estimator correction
also changes entries and must be qualified beyond this set before live adoption.
Full live tracing and replay of the two old live losses remain outstanding because
their original raw causal input history was not saved.
