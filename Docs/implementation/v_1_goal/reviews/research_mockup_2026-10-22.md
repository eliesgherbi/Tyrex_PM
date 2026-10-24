# Research workbench mockup review — 22 October 2026

This is a review aid, not another roadmap. [research_IHM_enhancment.md](../research_IHM_enhancment.md) is the single design reference. Its phases, vocabulary, open decisions and gates take precedence over earlier mockups and proposals.

The mockup was compared with the current `frontend/src/App.tsx`, `DataLibrary.tsx` and `Operations.tsx`. The **Current frontend** switch is a simplified representation of those files, not a captured screen or a claim that the proposed behavior is implemented. All numerical results and traces in the target are illustrative.

## Keep

- The Tyrex shell and Data / Research / Runs / Evidence navigation.
- Data Library's recording-first layout, chronological cards, selection persistence, saved draft, window inspection, pagination and preparation-plan flow.
- Existing Experiment and Candidates entry points, the shared workflow operation boundary and the current strategy/replay engine.
- Qualification/limitation fields, unknown-result handling, and separate operational launch gates.

## Move

- Surface the strategy schema and base configuration in Experiment. User-visible resolved parameters replace the current empty-parameter submission; notebook-era overlays do not remain a hidden second configuration.
- Move per-window results out of the bind/run panel into Results, with per-configuration summaries, the window table and the decision/execution timeline.
- Put comparisons in a dedicated view reachable from a result. Compare is a view across results, not an experiment mode.
- Start candidate creation from a selected result row. Preserve its resolved configuration, origin/result ids, rationale and any development/holdout numbers. Keep the Candidates page for review, freeze and export.
- Keep run progress, stop and resume in an experiment's run panel. The previous mockup's extra “Replay jobs” top-level Research tab is removed. The existing workspace Runs page remains the operational paper/live area.

## Change

- One named policy drives Data coverage, the selection funnel, preview and saved set. Its accepted K/outcome/payout assumptions are visible and stored explicitly.
- A window set has no partition or development/holdout identity. Legacy split-file gates and reserved-market labels do not appear in the target set flow.
- Replay uses the whole set, one or several explicit configurations and a scenario; it has no objective, split or ranking. A candidate saved from it says “no holdout evaluation.”
- Tune owns its grid, objective, chronological per-recording holdout lists and top-k. Only the development top-k proceed to holdout. Ranking, Generalisation and Stability are separate result views. The mockup refuses a zero-percent Tune holdout and does not offer candidate selection from non-top-k rows.
- Summary P&L uses qualified rows only. Unqualified numeric values remain diagnostic and unresolved values stay unknown.
- Candidate export shows preserved and explicitly added operational fields. Research-only tolerances are not silently inherited; launch remains separate.

## Phase boundaries shown in the review

- **Phase 1:** retain Data Library interactions while adopting one policy and plain window sets.
- **Phase 2, default target:** Replay, base/schema parameters, run preview and resume, Results, Compare, result-linked candidate, freeze and paper export report.
- **Phase 3, optional review switch:** Tune inputs, two-stage run, development ranking, generalisation, stability and candidate-origin differences. This switch previews the future design; it does not say Tune is implemented.
- **Later:** notebooks open workflow objects by id; paper launch remains under its unchanged evidence requirements.

The mockup includes a review-only `strict_v1` option and a proposed editable top-k default of 3. The canonical document still lists policy availability, the final objective list and the top-k defaults/editability as open decisions. The prototype does not resolve those decisions.

## Verification for this design revision

An isolated headless browser checked current/target switching, explicit base-field updates, simulated stop/resume, results and input-gap inspection, Replay candidate provenance, rationale/freeze/export, Tune zero-holdout refusal, top-k restrictions, experiment-owned stage membership, policy eligibility, review-note visibility and mobile layout. These preview checks passed with no JavaScript errors. They are not framework execution or qualification tests.

Only the review artifacts and a supersession note on the historical 21 October proposal were changed. No frontend/backend implementation, saved set, experiment, result, split file, recording or trading session was changed or launched.
