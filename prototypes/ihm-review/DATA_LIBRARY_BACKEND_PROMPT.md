# Prompt for the backend agent

Update Step 3's implementation contracts and acceptance checks for the agreed Data Library workflow.

Read `Docs/implementation/v_1_goal/03_reusable_workflows.md`, `step_03_implementation_record.md`, `step_03_data_library_extension_proposal.md`, and the relevant workflow/warehouse code. Inspect the implementation rather than assuming the existing packet status proves the full journey.

The frontend is being redesigned separately in `prototypes/ihm-review/`. Do not edit that prototype. This task updates specifications and the implementation backlog only: do not change backend code, process data, launch collectors, run research/paper sessions, or send orders.

The intended journey is:
Warehouse → choose a data scenario → scope/composition → review a selection preview → save an exact window set → open an experiment using its selection ID.

Keep these meanings distinct:
- Data scenario recipe: a versioned question and editable selection rules.
- Window set: the UI name for an exact saved input selection.
- Execution scenario: replay latency, fees, fills and other execution assumptions.
- Experiment: binds the set to strategy settings, execution scenarios, baseline, metric definitions, acceptance criteria and resource limits.

Specify the smallest contracts needed for:

1. Read-only warehouse discovery: paged windows, date/coverage queries, per-window detail and timeline tracks for recording, normalization, Up book, Down book, reference, quality and outcomes. Declare bounds, timezone, aggregation, freshness, unknowns and unavailable sources. Reads must not publish data, create workflow directories or rebuild indexes implicitly.
2. Real selections: replace the empty placeholder-manifest behavior with actual manifests/checksums, source choices, policies and warm-up dependencies. Refuse an unexpectedly empty pinned catalog. Preserve immutable versions and expose legacy limitations.
3. Recipe resolution and preview: eligible windows, manual cases and missing-input diagnostics. Return inclusion/exclusion reasons, limitations, ambiguities, preparation needs and a preview identity. Saving must use and validate the preview reviewed. Volatility/spread/liquidity filters stay unavailable until supported measurements exist.
4. Experiment binding: resolve a saved selection ID into validated replay units on the backend. The UI must not supply internal units or reconstruct pins. Validate strategy support, scenario contents, counts, budgets and identities before launch.
5. Managed preparation: preview the actual processing scope, reuse existing normalization/quality/verification owners, report durable progress, respect collector ownership, and define safe stop and compatible restart capabilities. Do not promise unsupported fine-grained pause/resume.
6. A thin local UI transport around shared operations. CLI, notebook and frontend must use the same business rules and structured errors.

Protect evaluation partitions across duplicate recordings and through filters, counts, labels and previews. Keep retrospective outcomes outside causal inputs. Proxy, provider-confirmed and unknown labels remain distinct.

Organize implementation into reviewable increments: contracts; read-only inventory; real selection pins and recipe preview; experiment binding; managed preparation; interface integration. The warehouse view may connect once its own contract passes. Paper launch, recording-session proof and ask70 capture retain their existing gates; they do not block browsing existing recordings.

Define acceptance checks for each increment, including:
- Timeline/list/detail consistency and side-effect-free reads.
- Fresh-process reopening of real pinned data after a later publication; missing or changed inputs refused.
- Preview/save consistency and explicit duplicate/reference choices.
- No duplicate processing owners; safe interruption and restart.
- A small recorded-data selection reaching replay without manually supplied units.
- Matching CLI/notebook/frontend plans and no holdout leakage.

Update the implementation record without rewriting historical evidence or claiming new acceptance. Keep completed foundations, newly identified gaps and deferred gates separate. Produce a concise implementation backlog with dependencies and the smallest first vertical slice.

Return the changed document paths, proposed operation/schema contracts, unresolved design decisions with recommendations, and the acceptance criteria for the first backend increment. Do not implement the backlog yet.

Frontend reference: `prototypes/ihm-review/DATA_LIBRARY_REVIEW.md`. Its mock IDs, hashes and progress animations demonstrate the screen contract; they are not backend artifacts or qualification evidence.

User refinement: also incorporate `prototypes/ihm-review/CUSTOM_FILTERS_BACKEND_NOTE.md` into the DL-2 recipe/scope contracts and acceptance checks. Custom sets need outcome/provenance and daily time/zone/weekday filters, all-matches selection with optional picks, reusable versioned rules, and explicit future measurement support. This remains a specification-only update until implementation is separately authorized.
