# Workspace follow-up: from saved windows to strategy results

> **Historical proposal.** Superseded on 22 October by [Research IHM enhancement](../implementation/v_1_goal/research_IHM_enhancment.md), the single current reference. In particular, window sets have no partition, Replay and Tune are explicit experiment modes, and only Tune owns a development/holdout split. See the [22 October mockup review](../implementation/v_1_goal/reviews/research_mockup_2026-10-22.md) for Keep / Move / Change annotations. The original text below is retained as history.

21 October 2026. Design proposal based on inspected source and current implementation records. No replay, recording or trading job was launched for this review. The other implementation work can continue independently; this proposal does not modify `frontend/` or `prototypes/ihm-review/`.

## Reminder: what is already in place

- Data Library reads warehouse recordings, coverage tracks and individual windows, and supports reviewed recipes/custom selections, preparation jobs and pinned saved window sets.
- Saved selection → `experiment.bind` → backend-built replay units exists. Jobs have background execution, preserved parts, progress and ownership/stop/resume mechanisms.
- The integrated Research screen can bind a saved set, start replay, open basic result rows and query trace availability. Candidates, paper preflight and evidence have initial screens/contracts.
- The real strategy/replay engine and richer notebook explorers already exist and should remain the behavioral authority.

Implementation and fixture checks do not equal full end-to-end acceptance. The implementation record reports a real exploratory 95%-coverage set with 15 windows from the 6 October recording; complete coverage selected zero. That pass did not replay the set. Its earlier bound experiment also needed rebinding to refresh threshold-quality acceptance. These details may change as ongoing work progresses; consult the latest saved experiment and manifest before execution.

The ML lab is a separate track. This checkout does not yet contain `research/ml_lab`; a learned artifact must therefore remain unavailable in the UI until delivered and validated. Analytical-strategy replay can move forward independently.

## Keep the navigation, refine Research

Keep **Data / Research / Runs / Evidence**. Within Research use:

1. **Experiment:** choose a saved set, named strategy configuration(s), probability source and execution scenario(s). Show effective parameters, reference/book/fee policies, label/coverage limitations and intended population. Preview `windows × configurations × scenarios` before launch. Carry the selected set directly from Data; do not repeat the selection wizard here.
2. **Replay jobs:** show progress, current block/unit, completed/failed counts, last update, stop/resume availability and links to preserved outputs. Offline jobs are separate from paper/live sessions under Runs. A finished job is not automatically a qualified result.
3. **Explorer:** start with the full population, then drill into a window and decision. Keep configuration/scenario/set identity visible. Show fill/no-fill/no-entry/partial/unresolved states, fees and exposure alongside correctly defined PnL. Align model point/bands, token bid/ask, input gaps, strategy decisions and execution events on one timeline. Preserve missing model intervals. Display held-side probabilities using the correct complementary bounds.
4. **Compare:** pair by market and configuration/scenario identities, and disclose overlap/exclusions and unknown values. Explain the parameter differences. Open a changed window directly in the Explorer. Do not compare two totals from different populations as a paired improvement.
5. **Candidates:** save the exact explored configuration, artifact identity if any, source experiment/results, data pins, limitations and rationale. Freezing and untouched evaluation are later explicit steps; candidate save does not select a production winner.

Runs remains the operational view for paper/live status, accounts, exposure and blockers. Evidence should be reachable from each limitation as well as from navigation; long qualification details need not dominate the main research flow.

## What to change from the earlier mockup

- The saved window set becomes the stable context throughout Research. Name, immutable version, membership count and population remain visible.
- Replace free-floating demo selectors with experiment/result identities and effective configuration values. A changed setup creates a new experiment; viewing historical output never recomputes it implicitly.
- Distinguish decision, order submission/arrival, fill, hold and resolution. A request marker cannot stand in for a fill.
- Keep market outcome separate from trade PnL; a predicted winner and a profitable trade answer different questions.
- Retain skipped windows and unresolved exposure. Summary denominators and missing-value rules remain visible.
- Add a probability-source slot now, defaulting to analytical. Learned terminal models become selectable only when a compatible frozen artifact exists; do not imply that the ML implementation is complete.
- Keep illustrative design data explicitly separate from real saved results.

## Necessary backend work before a rich Explorer

Current `workflows/worker.py::_strategy_replay` runs the real driver but returns a compact summary. The worker does not currently publish the decision sidecar that `results.trace` expects. The integrated frontend consequently reports availability rather than drawing a saved trace.

The next implementation slice should preserve a versioned per-unit result bundle: manifest, complete identity, economics summary, decisions, execution ledger/events, and the quote/reference/feature observations needed to explain decisions. Reuse the existing replay result and timeline schemas. Publish atomically with checksums; keep large traces separate and load them only for the selected window. A missing trace stays unavailable, not a synthetic reconstruction presented as recorded output.

Full-population aggregation also needs a deliberate operation. Current `results.open` paginates rows and `results.compare` forms overlap from the returned page; it is not yet a full paired-analysis service. Compute summary/paired metrics over every relevant saved part, independently of the visible table page. Preserve qualified/unqualified economics separately and expose counts of unknown results. Check compatibility and report any scenario/model/data differences explicitly.

The current candidate screen submits `parameters: {}`. Replace that UI path with the selected result configuration plus origin/evidence links before presenting candidate save as preserving an explored strategy.

## Delivery order

1. Prove one saved, pinned exploratory set → explicit strategy profile → replay → reopened result bundle, without manual units or synthetic traces.
2. Complete experiment preview and replay-job navigation around that path.
3. Add full-population summaries and one-window Explorer with linked decision/execution inspection.
4. Add complete, paired comparisons and candidate provenance.
5. Integrate a learned probability artifact when the separate ML work is ready, then progress paper/live pages according to their own evidence gates.

Acceptance starts with one correct, inspectable journey. Additional charts and parameter grids follow once saved results support that journey.

## Evidence inspected

- [Data Library implementation record](../implementation/v_1_goal/step_03_implementation_record.md) and [latest backlog/status](../implementation/v_1_goal/step_03_data_library_backlog.md).
- [Current workspace operations](../../frontend/src/Operations.tsx), [experiment binding](../../src/tyrex_pm/research/workflows/experiments.py), [replay worker](../../src/tyrex_pm/research/workflows/worker.py), [result operations](../../src/tyrex_pm/research/workflows/results.py).
- [Task A ML plan](btc5m_ml_lab_terminal_probability_plan_2026-10-20.md).

The accompanying interactive design uses eight explicitly illustrative windows. Its numbers and traces are not output from the saved 15-window set or evidence of strategy performance.
