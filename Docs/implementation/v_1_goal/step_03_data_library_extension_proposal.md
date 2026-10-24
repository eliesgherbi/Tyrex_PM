# Proposal — Data Library and scenario workflows

Date: 21 October 2026. Status: review proposal, not an implementation instruction or acceptance of Step 3. Code inspection only; no processing, collection, replay, or trading was started. The existing Step 2 follow-up remains deferred.

## Intended outcome

A user can see what the actual warehouse contains, understand which observations and outcomes are trustworthy for a stated use, prepare recorded inputs, and build a named, reproducible window set for a research question. The set opens in the existing experiment workflow without writing paths, manifests, or worker units by hand.

Inspiration: CYBELIA/SOCRATES separates source inventory and ingestion from goal-driven dataset assembly. Its implemented wizard uses Goal → Scope → Composition → Save. Its proposed roadmap treats a scenario as a question plus an evaluation policy, with explicit evidence, baseline, metrics, budgets, and limitations. The roadmap is a design reference, not proof that all its planned capabilities are implemented.

Reference sources:

- `E:/work/cybel_AI/DTX_SOC/cyberfront/src/features/data/DataLibraryPage.tsx`
- `E:/work/cybel_AI/DTX_SOC/cyberfront/src/features/data/SourcesPanel.tsx`
- `E:/work/cybel_AI/DTX_SOC/cyberfront/src/features/data/wizard/GoalStep.tsx`
- `E:/work/cybel_AI/DTX_SOC/cyberfront/src/features/data/wizard/ScopeStep.tsx`
- `E:/work/cybel_AI/DTX_SOC/docs/ROADMAP.md`, B1–B5 and X1

## Vocabulary and ownership

- **Collection:** observations associated with a saved recording profile. A **session** is an actual start/stop period. Collection, market, and session counts are different quantities.
- **Data scenario recipe:** a versioned question and editable selection rules. Records intended use, source scope, input requirements, label policy, partitions, optional sampling limits/seed, and limitations. Its matches can change when reevaluated.
- **Window set:** the UI name for the existing saved input selection. Exact membership and source versions are immutable. A recipe is not itself this resolved set.
- **Execution scenario:** replay assumptions such as latency, fees, and fills. Existing execution-scenario objects retain their meaning.
- **Experiment:** binds a window set to strategy/configurations, execution scenarios, baseline/comparison choices, resource limits, and requested outputs. Evaluation metrics/criteria belong here; the data recipe may suggest defaults but does not silently launch an experiment or grant qualification.

Use distinct identifiers and fields for data recipes and execution scenarios. Do not rename the existing execution `scenarios` field to mean data filtering. No new top-level scenario orchestrator is necessary for the first increment.

## User journey and page responsibilities

### Warehouse

Show all configured roots, with unavailable roots/errors visible rather than silently counted as empty. Filters: asset, market duration, collection/session, date/time interval, source type, preparation/coverage state, intended use, and partition/prior use. Declare UTC storage, displayed timezone, half-open time bounds, and whether a filter selects window starts or overlaps. Recommend window-start filtering with warm-up/context loaded separately.

Timeline: aligned recording, normalization, Up book, Down book, required reference, quality-publication, and outcome-availability tracks. Coarse buckets are summaries, not evidence of full coverage. Return bucket counts/denominators, unknown states, query bounds, aggregation semantics, source/publication identity, and last assessment time. Zoom/drill-down reveals exact intervals and gaps. Distinguish expected-but-unrecorded, not expected, present-but-unassessed, and known unusable data. Only infer missing expected windows where an explicit schedule exists.

The window inspector exposes input intervals, warm-up coverage, duplicates/alternative recordings, reference-series choices, label provenance, semantic limitations, and links to evidence. Trading captures remain distinct from collector archives. A stale heartbeat never proves a collector is alive.

### Scenario → window set

1. Pick a question/template or create custom rules.
2. Choose scope and intended use.
3. Set requirements, explicit source/reference choices, and optional composition rules.
4. Preview included/excluded windows, reasons, unavailable measurements, outcome provenance, preparation needed, and partition restrictions.
5. Explicitly prepare missing derived data if desired, then refresh the preview; preparation may alter availability and requires a fresh preview identity.
6. Save a named window set from that preview. Revalidate its exact inputs; either preserve the pinned snapshot or report a mismatch. Never silently substitute newer data.
7. Open an experiment using its selection ID. Review strategy, execution assumptions, counts, resource budget, and comparisons before launch.

Initial recipes: all eligible windows in a scope; custom/manual selection; missing-input diagnostic. Add volatility/spread/liquidity recipes only when their measurement definition, units, observation interval, source identity, and availability are implemented. Preserve whole windows and their required prior context; do not sample individual messages to mimic a valid book replay.

Outcome-conditioned/balanced sets are diagnostic/development selections and must not be described as representative. Unknown outcomes remain unknown. Reserved outcome counts, filters, previews, and examples follow the existing evaluation guard; a different collection of the same market does not bypass it. Outcome/after-close information is not a decision-time feature.

### Preparation and recording

Preparation is contextual to selected gaps: preview operations and actual processing scope, explicitly start, observe progress, and reopen the result after disconnect. Existing work is reused. Respect collector-owned normalization/quality locks; do not start duplicate writers. If the owner works collection-wide, display that even when a small date range is selected.

Normalize, publish quality, and verify remain separate outcomes: worker completion is not market suitability or qualification. Cooperative stop occurs at a safe owner-supported boundary; do not promise fine-grained pause/resume where unsupported. A crash can restart from committed owner work after lock/identity checks.

Record new data uses existing collection controls and profile limits. It creates a recording session, not a window set. Resume does not backfill unobserved historical events. Historical backfill, if ever supported, needs its own explicit source/provenance contract and is outside this first increment.

## Findings against the current implementation

These are source-inspection findings, not newly executed test results.

1. `library.collections` lists collections/sessions. It does not provide the full paged warehouse window inventory, date/coverage queries, or timeline aggregates required here. `library.inspect` inspects a saved selection, not arbitrary warehouse windows.
2. `library.prepare` delegates normalize/quality/verify synchronously. Reuse those owners; add preparation planning and durable orchestration/progress rather than keeping a browser request open. Current research job unit kinds do not implement preparation.
3. `selection.resolve` already distinguishes duplicate collections, reference series, and trading-capture provenance. Preserve these policies and extend ambiguity handling where needed, including multiple sessions/builds within a collection.
4. `selection.save` currently always stores the empty `_pin_manifest()` placeholder, with caller-supplied `pin_parts` stored separately. Existing byte-pin fixtures do not prove a saved selection reopens real normalized window data. `selection.open` also conditions missing-market detection on the catalog being nonempty. Real manifest/context pinning and an empty-catalog refusal are required before claiming warehouse-backed reproducibility.
5. `experiment.save` stores caller-provided `units`, `pins`, and an optional selection ID. It does not resolve that selection into executable units. A saved experiment without units is refused by `job.start` as `empty_plan`. The browser should not construct these internals; the backend must bind and validate the selection, actual manifests, configurations, and execution scenarios.
6. Existing detached research jobs, locks, atomic publications, source fingerprints, CLI/notebook boundary, and evaluation guard are foundations to reuse. Inspect and extend contracts rather than treating their fixture coverage as acceptance of this new journey.
7. The frontend review prototype uses synthetic data and is not connected to these Python operations. A thin local transport is still required for integration.

## Proposed backend boundaries

Names below are illustrative additions/extensions to the existing workflow catalog, not currently available operations.

- **Inventory:** `library.windows`, `library.coverage`, and a window-detail operation. Bounded/paged queries over committed manifests and published assessments; explicit freshness, incomplete publications, source IDs, and filter semantics. Reuse Reader/WindowCatalog/quality projections; no new storage engine. A disposable overview index can accelerate reads but never replace archive identity.
- **Recipes:** `data_recipe.list/resolve/save/open` where needed. Start with a small supported template catalog plus custom rules; version resolved rules and refuse unsupported filters. A recipe cannot accept caller-declared readiness as evidence.
- **Selection preview/save/open:** server-resolved inclusion/exclusion, source choices, use-specific assessment, preview identity, real pinned manifests/parts, policy versions and required context. Extend existing selection operations. Preserve old records with explicit legacy limitations; no bulk migration or silent evidence upgrade.
- **Preparation:** preview/start/status/stop around existing owners; use shared job/lock primitives where appropriate. Record collection-wide scope, owner acknowledgement, safe stop capabilities, progress units, committed outputs, errors, and compatible restart. Keep preparation and experiment job types explicit.
- **Experiment binding:** selection ID + saved configuration(s) + execution scenario identity → validated real replay units and exact counts. Fingerprint scenario contents, policies, dependencies, relevant code, and manifests. Refuse unsupported strategies and mismatched inputs before execution.
- **Local UI transport:** thin handlers for catalog operations and structured errors/progress. Backend owns path validation, configured roots, guards, schemas, and work. Reads must not start jobs, mutate workflow directories, publish quality, or rebuild indexes implicitly. Writes remain explicit and bounded. No shell-command construction in the browser.

## Step 3 changes and implementation order

This is a targeted completion/extension of W1–W2/P1 plus the P2 experiment binding and P4 controls. It does not require rebuilding results, strategies, notebooks, or the trading host.

1. **Agree contracts and vocabulary.** Update W1/W2, operation catalog proposals, saved-object schemas, and Step 4 screen bindings. Record recipe versus execution-scenario meanings and use-specific readiness.
2. **Deliver actual inventory and coverage.** Read-only warehouse page, paged windows, date filters, drill-down, assessment freshness, explicit unknowns, and protected labels. Acceptance: explain one complete and one incomplete real recorded window from a fresh process without archive changes.
3. **Deliver exact window sets and binding.** Goal/recipe preview → named selection with real pins → experiment units. Acceptance: a small eligible recorded selection runs through the existing replay adapter with no manually supplied units; after later publication the old set remains identical, while missing/changed inputs block. A tiny committed fixture archive can prove mechanics; also retain one explicitly authorized existing-archive integration result.
4. **Deliver managed preparation.** Preview/start/status/safe stop/restart, fresh process/browser reopen, and busy-owner refusal. Acceptance: prepare a deliberately incomplete isolated fixture archive and show changed derived readiness without modifying raw observations or launching a collector.
5. **Wire the UI and prove the vertical slice.** Warehouse → scenario → preparation if needed → saved set → experiment preview → run → reopen result. CLI and notebook use the same operations. Preserve custom notebook research and existing immutable bundles.
6. **Qualify recording controls separately.** Retain P4's outstanding isolated real-session proof. Paper launch and ask70 capture/reconstruction keep their existing Step 2-related gates. Reading and preparing existing recordings do not inherit those paper-trading gates.

Recommended first milestone: actual warehouse visibility plus one custom/eligible-window scenario and one reproducible real-data handoff. Add further recipes only when they answer a concrete question and have supported metadata.

## Required verification

- Timeline/list/detail agree on the same scope, bounds, duplicates, timezone and assessment identity; unknown is never complete or zero.
- Busy workers, stale status, safe stopping, interrupted publication and compatible restart cannot create two writers.
- Real window manifests and warm-up inputs survive reopen in a fresh process; latest publication is not silently adopted; an unexpectedly empty pinned catalog fails.
- Selection/recipe changes create new identities and invalidate affected previews; experiment units/counts derive from saved inputs.
- No outcome leak through reserved filters, counts, timeline colors, duplicate recordings or previews. Unknown/proxy/attested labels stay distinct.
- Browser, CLI and notebook resolve the same set and experiment. Browsing does not write artifacts or start preparation/collection.

No new tests or runs were executed to produce this proposal. Prior implementation-record test claims remain scoped to the checks they actually describe.
