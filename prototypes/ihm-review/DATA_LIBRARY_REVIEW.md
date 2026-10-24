# Data Library screen contract — review sketch

Date: 21 October 2026. All data is synthetic; no warehouse read, preparation, recording or research is invoked. This is the frontend design companion to the Step 3 data-library extension proposal. It does not authorize or implement backend work.

## Views

- Warehouse: source, BTC five-minute market, UTC start/end-exclusive date filters, availability filter; scope counts; seven-track window-start availability overview; window table with selectable development cases; evidence drawer with warm-up, outcome provenance, source/session, per-window intervals and gap reasons.
- Window sets: browser-local named sets with immutable membership snapshots, preview IDs, recipe/use/source choice, inspect and experiment handoff. No set is pre-created by the application. The former fixed “Pin eight development windows” action is removed.
- Recording sessions: explains collection versus session and processing versus capture. Static metadata; no inferred current collector state. New recording remains unavailable until integration and its own proof.
- Builder: Goal → Scope → Composition → Save. Eligible-window, custom-window-set and missing-input recipes. Custom sets use outcome, provenance, daily clock/time zone and weekday filters, with all matching windows by default and optional individual picks. Unsupported market-condition recipes explain their missing measurement. Composition declares books/reference/warm-up/outcome requirements, label policy and explicit alternative-source choice. Every exclusion has a reason; reserved outcomes remain hidden.
- Experiment handoff: carries the chosen demo set ID/name/membership/preview into Research → Experiments. Baseline/configuration count, execution scenario, metric denominator explanations, acceptance criteria and bounded work preview appear before results. Run remains unavailable. Existing Explorer fixtures are a separate result population and are never assigned to the newly selected set.

## Interaction boundaries

- `dataLibraryMock.ts` is an explicit local fixture adapter, not the future API. Its v3 examples contain 42 windows across three illustrative days and several daily hours. Original v2 identities/timestamps are preserved. Timeline columns are window starts, not a claim of continuous recording between sampled times.
- Availability is independent from outcome direction, outcome provenance and original strategy actions. Input-ready with unknown/unresolved outcome is possible unless known scoring outcomes are explicitly required.
- Date filters select starts in `[from, until)`, UTC. Context is additional. Counts, table and heatmap use the same source/date/availability view, with scope statistics explicitly preceding the availability filter.
- Selection-save uses the current preview membership and captures a local preview identity. New filters/preparation do not change an existing set. Actual manifests/checksums/context pins and save-time validation remain backend requirements.
- Preparation preview shows target windows and collection-wide owner scope. The three-stage animation updates only local fixture normalization/quality; it cannot repair book/reference/warm-up gaps. Stop/resume is a browser UI demonstration only, with no promise about production owner granularity or persistence. Reset processing demo restores the initial fixture assessments.
- Preparation state and saved sets persist only in the current tab's sessionStorage. Browser-only IDs and the small illustrative preview hash are not backend IDs or cryptographic pins.
- No backend URLs, credentials, shell calls, data files or trading controls are used. Disconnected states must remain explicit until their individual contracts and evidence are accepted.

## Backend handoff

Agree read-only inventory/detail/coverage responses; supported recipe and preview/save contracts; actual pinned selection records; selection-to-experiment compilation; durable preparation controls. Keep authoritativeness in Python and replace the fixture adapter after review. No new storage engine, copied warehouse or browser-authored replay units are needed.

See `DATA_LIBRARY_BACKEND_PROMPT.md` for the specification-only task to send to the other agent.

## Custom filters and reusable scenarios

Scope now supports market outcome (Up/Down/unresolved/unknown), label source, daily start/end, UTC or Europe/Paris clock and ISO weekdays. Dates remain UTC; clock/weekday filters use their declared zone. Daily ranges are half-open; start after end means overnight. Equal endpoints are invalid when enabled.

The matching candidate table exposes outcome and provenance beside every development window. All-matches selection avoids individual ticking. Optional picks intersect those filters and the input eligibility requirements. The preview distinguishes matching candidates from included windows and explains exclusions.

Outcome/label-source-conditioned sets are diagnostic; otherwise eligible diagnostic sets may still open an experiment draft, with their selection limitation retained. Reserved outcomes are excluded before matching/counting. Incomplete-input diagnostics remain blocked from ordinary replay. Saved sets retain a filter snapshot, and “Use filters for a new set” creates a new draft without changing prior membership.

`CUSTOM_FILTERS_BACKEND_NOTE.md` records the required contract additions. `validate-data-filters.mjs` checks clock bounds, DST/weekdays, outcome/provenance rules, protected-label short circuit and immutable filter snapshots. Reversal/volatility/liquidity measurements remain unavailable until explicitly defined and implemented.
