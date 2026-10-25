# Step 3 — Reusable workflows

> **History.** Superseded by [STATUS.md](STATUS.md) and `Docs/latest/`. Kept as the record of how this was decided.

Status: workflow specification. The 20 October 2026 review remains the original contract. The offline catalog implemented after that review is recorded in [step_03_implementation_record.md](step_03_implementation_record.md) and does not by itself accept the Data Library journey. Section 11, added 21 October 2026, is the agreed Data Library extension and supersedes W1–W3 where they differ on warehouse discovery, saved inputs, and experiment units. Step 2's remaining work stays deferred. This document update authorizes no processing, collection, replay, or trading.

Review companion: [Step 3 workflow proposal](C:/Users/windo/.cursor/projects/e-polymarket-Tyrex-PM/canvases/step-3-workflow-proposal.canvas.tsx). Codebase findings: [Step 3 codebase review](C:/Users/windo/.cursor/projects/e-polymarket-Tyrex-PM/canvases/step-3-codebase-review.canvas.tsx). This document is the work specification. Canvas notes are retained discussion history; changed proposals require reconfirmation rather than silently inheriting an earlier agreement.

## 1. Goal and release boundary

Make this user journey repeatable through shared operations:

**Data library → saved input selection → experiment definition → preview → job → results → candidate → paper profile and rehearsal.**

Users should not need to reconstruct commands, notebook switches, paths or kernel state. Existing notebooks remain available for teaching and custom research. CLI, notebook adapters and the later interface call the same operations. The existing host remains the only order-submission path.

The first release covers current BTC five-minute settlement research: fixed/list/bounded-grid experiments, saved result exploration, candidate/profile preparation, and paper capabilities with accepted Step 2 evidence. Demonstrate shared **host recording with ask70**, using the host recording block and supported reconstruction after a valid ask70 paper profile exists. This is not an ask70 strategy-lab adapter. A complete second-strategy lab or a z_gap alternative is outside this packet.

Outside this release: new trading rules, ML training, distributed job infrastructure, a new storage engine, arbitrary collection-profile editors, automatic candidate selection, live trading controls and redesigning every chart.

## 2. Can work start before Step 2 finishes?

Yes, by dependency. This replaces the former blanket requirement to wait for all of Step 2.

Current sequencing decision (20 October 2026): the user deferred the remaining checks in [Step 2 follow-up](02_step2_follow_up.md) while independent Step 3/4 work proceeds. Offline packets and their interface can be delivered first. P5's paper/restart/reconstruction acceptance and full trading handoff remain open until the relevant follow-up evidence is accepted. Do not fold these deferred fixes into unrelated workflow refactoring.

| Work | Can begin | Boundary |
|---|---|---|
| Schemas, operation contracts, read-only library, exact selections and offline availability reports | Before Step 2 completion | Fixed committed recordings; implementation isolated from the build under qualification |
| Offline experiment planning, result adapters, comparison, candidate records and paper-profile generation/validation | Before Step 2 completion | Pin inputs/builds; valid generated profiles are not automatically qualified |
| Shared replay/batch/explorer changes | Once affected interfaces are stable and changes coordinated | Do not edit files or caches used by an active Step 2 run; use isolated source and separate outputs |
| Collector/preparation wrappers | Once their data and ownership contracts are ready | Reuse supervision; serialize heavy capture/preparation against timing qualification on the same machine |
| Paper launch/restart/reconstruction integration and acceptance | After relevant Step 2 hold/recovery evidence is accepted | Optional retry/exit/re-entry retain their own capability gates; unrelated real-provider checks need not block offline work |
| Second-plugin demonstration | Once its shared contract is stable | Offline/fixture proof can precede paper proof; respect that plugin's supported lifecycle |

Check the inventory and evidence on disk before integrating. Do not infer success from elapsed time or this specification. If filled hold/restart evidence remains open, complete independent offline packets and leave paper acceptance open.

While another agent changes runtime/execution/replay, coordinate file ownership and isolate builds. Separate checkouts do not isolate common warehouses, ledgers, caches or result folders. New jobs must not compete with a measured Step 2 session. If isolation is unavailable, restrict parallel work to specification, static fixtures and read-only exploration.

## 3. Shared vocabulary and saved objects

These are proposed public meanings; historical artifacts are not automatically renamed or rewritten.

| Object | Meaning and change rule |
|---|---|
| Collection / session | Collection profile is in collections/<id>/collection.json; actual sessions are collections/<id>/sessions/<session-id>.json. Resume adds a session, not missing observations. Merged coverage spans are not sessions. |
| Market/window recording | One market identity with a selected collection/source version. Duplicate recordings are not independent outcomes. |
| Data scenario recipe | Versioned question and editable selection rules: intended use, scope, input requirements, label policy, partition, and optional sampling limits. Reevaluation can match new windows. The interface calls a saved exact selection a window set; this recipe is not that set. |
| Window set / input selection | Exact membership, chosen source builds, manifests, policies, warm-up dependencies, and exclusions. Immutable. A later publication does not change it. "Window set" is the interface name for this object. |
| Execution scenario | Replay assumptions: latency, fees, fills, and related execution policy. This is not a data recipe. The experiment field `scenarios` keeps this meaning. |
| Experiment definition | Binds one window set to strategy settings, one or more execution scenarios, a baseline, metric definitions, acceptance criteria, and resource limits. Editable draft; immutable saved version. The recipe may suggest metric defaults; it does not launch work or grant qualification. |
| Job / result bundle | Job is execution with durable ownership/progress; results are preserved outputs. Neither is an input dataset or trading episode. |
| Candidate / freeze | Human choice linked to evidence and rationale. Freeze locks the intended evaluation identity; it does not grant execution qualification. |
| Run profile | Complete host configuration translated from a candidate and explicit operational settings. |
| Trading episode / attempt | Existing Step 2 meanings. Retry stays within an episode; re-entry starts another after confirmed closure. |
| Reconstruction | Reproduce a particular captured run's decisions/context. Alternative-fill replay asks another question. |

Saved objects carry schema version, stable ID/content identity, creation time, parent references and originating operation. Separate display names/local paths from logical identity.

### Storage defaults

- Workflow records: 'var/research/workflows/', with selections, experiments, jobs, comparisons, candidates and profile-translation records. Keep results inside the existing 'var/research' holdout scan deliberately; compatible summaries retain market_id. Results may reference existing immutable bundles rather than copy them. Interrupted evaluation/diagnostic use also needs guard-visible usage records (section 7).
- Generated paper YAML: 'config/runs/coock_book_conf/generated/'. Versioned names; never overwrite reference or cookbook profiles.
- New paper state/evidence: 'var/research/workflows/paper/<run-id>/'. Account, ledger and report identities are isolated and satisfy existing paper-path validation.
- Existing warehouses and experiments stay in place. Discover configured roots, initially including current local roots; no hard-coded single collection.
- Read legacy artifacts through explicit adapters with limitations. No bulk migration or overwrite is required.

## 4. Common operation contract

Names below are proposed callable boundaries, not existing CLI commands. Extend the smallest current owner; no new service is assumed.

Every operation specifies:

1. **Request:** explicit input IDs, resolved settings, intended use, resource/recording limits and output location.
2. **Result:** output references, counts, exclusions, warnings and supported evidence claims.
3. **Identity:** exact data manifests/parts, relevant policies, plugin/configurations, scenario contents and the canonical source-tree content fingerprint, including uncommitted bytes. Git HEAD/dirty are display provenance, not the resume key. Extend the hashed sources for new workflow/adaptor code before trusting resume.
4. **Progress/errors:** current unit, planned/completed/failed counts, last update and the failing input/reason. Return structured support/compatibility/error codes and field-level mismatches; never hide incompatibility in a status-text suffix. Computation completion and trading qualification are separate.
5. **Ownership:** allowed writer, proof that a previous owner stopped and safe stop behavior.
6. **Repeat/resume:** reads are repeatable; identical saves may reuse immutable objects; changed content creates a version; incompatible resume is refused.
7. **Human action:** collection/job start/stop, candidate choice/freeze, opening reserved evaluation and paper launch remain explicit.

Publish atomically and reuse bounded processing. Credentials use existing environment/configuration mechanisms, not manifest or profile copies.

## 5. Eight workflow actions

### W1 — Data library and collection

**User intent:** find recordings, inspect coverage and collect more when needed.

- Inputs: configured roots, collection.json, sessions/<id>.json, committed normalized builds and quality/integrity evidence through Reader and WindowCatalog. Do not use Lab.recorded_intervals as a session catalog; its ten-second gap merging describes coverage. Trading captures remain distinct until explicitly imported.
- Outputs: filterable/paged collections, sessions and windows; source identity, processing age/status, coverage, labels, recorded prior use and evidence links.
- Inspect development windows through bounded book/reference/trade series. Label units, timezone, source and aggregation. Do not load every raw message to draw an overview.
- Offer missing normalize/quality/verify work explicitly and reuse valid completed processing. The default collector already supervises normalization/periodic quality; do not start duplicate workers.
- Wrap Collector and resume_profile in data_warehouse/supervisor.py for supported profile start/resume/status/controlled stop, retaining Ownership. run_data_collection_live is the existing launcher; tyrex-data has no collection start/stop subcommand. Add the control boundary rather than pretending those commands already exist. Arbitrary profile editing is deferred; controls follow read-only browsing as P4.
- “Browse and prepare existing recordings” means inspect data already on disk and finish missing derived processing. It does not start a feed collection, fill gaps or attest rules.
- Incomplete windows stay inspectable. Missing root, unsupported version, stale publication, ownership, credentials/provider and disk failures have specific reasons.

**Acceptance:** a fresh process finds a known collection, explains an incomplete window, identifies required processing and avoids duplicate workers. Section 11 replaces this acceptance for paged warehouse discovery, coverage timelines, and managed preparation.

### W2 — Explore and save an exact selection

**User intent:** select windows for a purpose and save reproducible inputs.

- Inputs: W1 inventory, intended use, filters, source-selection policy and development/evaluation membership.
- Outputs: versioned selection manifest with exact source/policy evidence and included/excluded counts/reasons.
- Initial filters: collection/session/date, source, market/window, required-input availability, label/threshold quality and partition/prior use. Outcome-dependent exploration is limited to development data.
- Apply three distinct source policies: (a) duplicate collection recordings of a market require an explicit chosen collection/session/build; (b) the reference series within a window, such as PolyBolt versus RTDS, needs its own compatibility/provenance policy; (c) trading captures are separate run evidence imported/reconstructed through recording reconstruct. None is a silent substitute for another. batch.window_inventory's sorted keep-first/duplicate_rejected behavior is legacy discovery, not the W2 selection rule.
- Pin committed manifests/parts, assessment/policy snapshots and lookback dependencies. Every reopen and worker passes the saved list to Reader(..., manifests=pinned_manifests), then constructs WindowCatalog and load_window. Lab.catalog currently opens latest committed builds; it must not reopen a saved selection. Collection ID alone is insufficient.
- Prove resolution after restart/new publication/compaction. Missing pinned inputs block rather than substitute latest data. Export only when a consumer needs a self-contained bundle.
- Archive integrity, coverage, strategy eligibility, label quality and attested semantics stay separate. Explicit proxy/provisional research acceptance does not upgrade evidence.
- Keep retrospective labels/later metadata separate from causal decision-time inputs. Partition membership applies to the underlying market across duplicates.
- A new recording changes a saved filter's matches, not an existing selection.

**Acceptance:** reopen the same selection from a fresh process despite added data; detect missing/changed parts and duplicate/unsupported sources; protect reserved evaluation outcome plots. Section 11 adds real manifest pins, preview identity, and the empty-catalog refusal. The empty placeholder manifest in the current `selection.save` does not meet that bar.

### W3 — Define an experiment

**User intent:** choose a strategy/template, settings and assumptions without editing notebook internals.

- Inputs: selection ID, registered strategy, fixed configuration/list/bounded grid, purpose, execution scenarios, workers and retained traces/explanations.
- Outputs: resolved versioned definition, defaults, explicit excluded combinations, reference differences and exact scenario/policy identities.
- Group settings into Data; Strategy; Execution assumptions; Experiment design; Job settings.
- Separate strategy retry/exit rules from account transport retries and simulated information/order/feedback delays, fees, participation and final-gate assumptions.
- Resolve through the settlement schema loader or explicit registry loader, reject unsupported lifecycle/protection combinations, and declare run/resource bounds before bounded Cartesian expansion. Do not call Lab.run, staged.Design, staged.preview, batch._run_group or run_batch as the new workflow: those paths use settlement-only defaults/axes or transit through Lab. Call strategy_replay.run_window with an explicit strategy_kind after loading pinned inputs; reuse/adapt committed part formats and digest rules.
- Registered kinds are ask70, feature_probe, settlement_value and z_gap. Only settlement_value is the initial workflow replay adapter. Registration is not lab/batch support. P6 uses ask70 host recording rather than claiming a second research adapter.
- Save new versions; preserve templates, frozen definitions and old experiments.

**Acceptance:** resolve one configuration and a small comparison/grid, inspect effective settings and reject incompatible combinations before work starts. Section 11 requires the backend to derive replay units from a saved selection id. Caller-supplied units remain a legacy fixture path and are not the Data Library handoff.

### W4 — Preview

**User intent:** inspect exactly what will run before explicit launch.

- Inputs: saved definition, pinned data and compatible completed parts.
- Outputs: windows × configurations × scenarios, exclusions, reusable/completed/remaining counts, reference differences, recording choices and estimates with their measured basis.
- Require preview for every launch. Launch consumes its plan identity; changed inputs require another preview.
- Diagnostic/development/frozen evaluation are purposes, not execution scenarios. Opening reserved evaluation uses explicit existing freeze rules.
- Do not call staged.preview or its seconds_per_run fallback as this operation. It is an edges-by-taus settlement preview and guesses 35 seconds without a timing sample. Return counts and structured compatibility; omit ETA/memory when measured evidence is absent, with an explicit unavailable reason. Reconstruction is not an alternative-fill scenario.

**Acceptance:** launched work matches preview; compatible resume subtracts completed work; changed code/config/data cannot silently reuse incompatible parts.

### W5 — Run, observe, stop and resume jobs

**User intent:** execute work, inspect progress/partial results and resume after interruption.

- Inputs: W4 plan ID, worker/output limits and job identity.
- Outputs: durable job/owner record, heartbeat, unit counts, errors, partial results and final output references.
- The local job mechanism is decided in section 7: one job directory, immutable request, OS-backed job lock, generation/owner identity, atomic status, explicit stop request and checksum-verified committed parts. Launch a worker independently of notebook/viewer lifetime; start returns a job handle only after ownership acknowledgement. Existing run_batch is an in-process pool, not this job system. Reuse its part conventions, not its caller lifetime.
- States: preparing, running, stopping, completed, failed, interrupted, cancelled. Completed means computation finished, not that a fill occurred or qualification passed.
- A stale heartbeat triggers owner inspection. Never create a second writer merely because a status record looks stale. Existing locks remain authoritative.
- Resume verifies data/config/scenario/policy/build identity and required trace outputs. Changed inputs create a new definition/job; old results remain readable.
- Research stop retains committed completed parts. Paper stop uses position-aware host shutdown. Trading restart retains account/ledger and is distinct from replay-part resume.

**Acceptance:** fresh versus interrupted/resumed work has equal logical outputs; another process reopens progress/results; incompatible resume and competing owners are refused.

### W6 — Explore and compare results

**User intent:** understand outcomes, compare settings and trace metrics to evidence.

- Inputs: saved bundles/identities, applicable labels and metric/view contracts.
- Outputs: overview, paired comparisons, window outcomes/distributions, timeline, explanation and execution/position views.
- Strict resume does not prohibit comparison. Different configurations are intentional. Show data/build/scenario differences and shared coverage.
- Default paired metrics use common underlying markets with declared matching assumptions. Broader side-by-side inspection is allowed but labeled unpaired/confounded.
- Every metric defines unit, denominator, eligible population and missing/unresolved handling. Unknown P&L is not zero. Show requests, submissions and fills separately, including no-trade windows and concentration.
- Computing or opening a comparison is read-only. Saving its chosen membership/settings as a comparison record is a separate explicit save operation; it does not rewrite result bundles.
- Read saved summaries/decisions/explanations/manifests through side-effect-free adapters; reuse frames.summary's missing-P&L and attempt/POST/fill distinctions. Explorer study loading may write study_index and timeline generation writes caches: neither belongs in open/compare. Verified read-only trace loaders may be reused. Diagnostic generation is a separate explicit operation with holdout guard and usage records; never rewrite the original result.

| View | Minimum meaning |
|---|---|
| Overview | Input/config/scenario scope, completed/failed work and limitations |
| Comparison | Changed settings, common-market count, pairing basis and metric denominator |
| Outcomes/distribution | Known P&L in USDC, inactive/unresolved windows, tails and concentration with counts |
| Timeline | Time with timezone, probability [0,1] if applicable, token price USDC/share, request versus fill markers |
| Explanation | Decision-time inputs/indicators, blockers, reasons and freshness |
| Execution | Attempts/POST counts, confirmed shares, debit/proceeds and remaining inventory |

**Acceptance:** reopen without a kernel, compare two configurations, inspect a window and reconcile a metric to contributing records. Do not expose reserved evaluation through incidental charts.

### W7 — Candidate, freeze and paper-profile generation

**User intent:** save a justified research choice and generate a complete paper profile.

- Inputs: configuration/result IDs, rationale, evaluation state and explicit operational paper settings.
- Outputs: immutable candidate, optional explicit freeze, complete paper YAML and translation report.
- Candidate choice, generic candidate freeze, evaluation access, profile generation and paper launch are separate actions. New candidate records use frozen_split and holdout_untouched for the canonical reserved split. Qualification.freeze and qualification.compare belong to the existing P7 sequence; do not subclass or call them as generic candidate/comparison operations. Software cannot prove every prior human inspection.
- Preserve explicitly chosen strategy settings and classify their origin. Do not merge Lab.RESEARCH_BASE_PARAMETERS into paper generation: its implicit acceptance of provisional K stays out. If a paper-specific tolerance is proposed, it must be an explicit labeled operational deviation. Show assumed PolyBolt/RTDS compatibility, tie rule, settlement provenance and fee curve separately; do not silently verify them or inherit Step 2 reach thresholds.
- Translation classifies preserved strategy fields, assumptions not transferred, operational additions and unresolved requirements.
- Supply explicit venue: paper, paper account/fill/latency/resolution settings, state/report paths, applicable settlement ledger, lifecycle/protection, recording, market selector/run mode, risk, account/clock readiness and run limits. Missing operational fields or unresolved semantic mappings block a runnable profile. Validate through runtime.run_config.load_trading_run_config and show qualification separately; schema validity does not attest semantics.
- Changing a candidate creates a version; original results/configurations remain unchanged.

**Acceptance:** generate/validate a profile and show exactly what was copied/added. Unsupported mappings block rather than silently dropping settings.

### W8 — Paper rehearsal and explaining a recorded run

**User intent:** operate a bounded paper session, inspect behavior and reconstruct eligible recordings.

- Inputs: validated profile, accepted relevant Step 2 recovery/evidence contract, bounded plan, recording choices and isolated account/ledger identity.
- Outputs: host-derived readiness, blockers, orders/fills/positions, summary and artifact links; separate reconstruction result where supported.
- Preflight distinguishes actual venue and mutation permission. Existing host owns all execution; no live submit control is added.
- Intentional restart retains account/ledger and verifies recovery prerequisites; fresh sessions receive separate identity.
- “Explain / verify this run” needs supported raw/context/clock evidence at captured instants. A summary alone is insufficient.
- Decision/explain recording and full raw diagnosis are distinct choices. Do not make raw capture mandatory for every research operation or conceal its costs/fault policy.
- A run missing its target event stays unverified. Qualification cites observed outcomes and comparison rules, not a successful process exit.

**Acceptance:** use the named S0 evidence in section 7 as historical observations, not a full pass. Its first restart stopped; the fixed replacement tracked the same ledger and rolled, but no S0 raw context exists for exact reconstruction. P5 requires the deferred follow-up's complete accounting-consistent hold/restart capture and reconstruction. Use tyrex-pm run --paper, watch-run (not watch), settlement status and recording reconstruct; no additional order path.

## 6. Logical object fields for implementation planning

Reuse existing files/manifests rather than duplicating authority.

- **Selection:** schema/ID/name, intended use, filter, per-market collection/source references, committed manifests/parts, lookback bounds, quality/label/partition policies, exclusions, parent and digest.
- **Experiment/preview:** selection, plugin/effective config IDs, scenario contents, execution/reference/book/clock policies, purpose, recording/resources/output settings, source fingerprint, planned unit IDs and bounds.
- **Job:** plan/kind, owner/heartbeat, state, expected/completed/failed units, committed parts, stop/error reason and output links. Separate scientific identity from runtime/hardware measurements.
- **Results/comparison:** parent unit/job IDs, source/config/data/scenario identities, view/metric schema, missingness/denominators, traces and common/excluded coverage.
- **Candidate/profile:** chosen config/results/rationale, freeze/evaluation links, operational translation, unresolved requirements, YAML hash, account/state identity and qualification links.
- **Paper/reconstruction:** run/capture IDs/checksums, actual mode, expected/observed scenario, comparison contract/fields/tolerances and remaining unverified claims.

## 7. Codebase constraints and decided implementation boundaries

These constraints incorporate the codebase review. Existing helpers are evidence to inspect, not blanket reusable operations.

### 7.1 Owners, commands and support

Use one new package, **src/tyrex_pm/research/workflows/**, with small modules as assigned in section 8. These paths are planned owners, not files already implemented. Ordinary callable functions own behavior; CLI and notebook clients delegate to them.

Three primary command families remain distinct:

- **tyrex-research:** keep the existing 'replay' command unchanged; it is the scripted-export engine in research/cli.py and research/replay. Add a separate 'workflow' namespace for W1–W7 and worker/job control. Do not route workflow replay through the scripted engine.
- **tyrex-data:** normalization, quality, verification, status, export, compact and list-markets. Collection start/resume currently use the separate run_data_collection_live launcher and Collector/resume_profile. Collection stop/control needs an explicit wrapper, not an invented existing subcommand.
- **tyrex-pm:** host run/observe/eval, watch-run, settlement and recording. P5 delegates to run --paper, --validate-config, watch-run, settlement status and recording reconstruct. 'watch' is not the current command name.

| Kind / surface | Initial support |
|---|---|
| settlement_value workflow replay | Explicit schema load, pinned Reader → WindowCatalog → load_window → run_window(strategy_kind='settlement_value', clock observations, scenario and execution policy). No hidden research-default merge. |
| ask70 host recording | P6 only: a new validated paper profile, required protection, flatten_before_close, single-window run. Existing ask70 example YAMLs are live, not reusable as-is for paper. |
| feature_probe / z_gap | Registered plugins; no new workflow-lab support claimed by this release. |
| Fixture eval / scripted replay / recorded-run reconstruction | Existing separate entry points and evidence purposes; neither is a synonym for closed-loop workflow replay. |

Lab.run always loads settlement configuration and merges RESEARCH_BASE_PARAMETERS. Lab.catalog opens latest committed data. staged.Design is a specific edges-by-taus design. batch.run_batch/_run_group transit through Lab. Preserve these notebook paths; do not turn them into the new public operation by renaming or wrapping them unchanged.

Reusable foundations are run_window, pinned Reader/load_window, scenario/config parsers, pure result projections, and committed part-file conventions. Adapt the existing part digest/format where needed, but extend its identity for pinned manifests, explicit plugin/effective config, scenarios, source fingerprint, recording outputs and schema versions. Old parts are reusable only when compatibility is demonstrated, not merely because the filenames exist.

### 7.2 Local research-job mechanism — D4 resolved

Existing run_batch is a caller-owned process pool with part-file resume. It is not a persistent job, owner or heartbeat. Implement the following minimal local mechanism in workflows/jobs.py, with record/publication helpers in artifacts.py:

- Directory: 'var/research/workflows/jobs/<job-id>/'. It contains immutable job.json (plan/request/digests), .workflow_job.lock, atomic status.json, committed parts/manifest references, and worker logs.
- Ownership: use the OS-backed StateDirectoryLock pattern on this job directory with a distinct lock filename; reuse the existing class where suitable. Warehouse Ownership and trading locks still govern their own resources. PID files and stale heartbeats do not grant ownership.
- Launch: a local Python worker runs independently of notebook/viewer lifetime. It acquires the job lock, verifies the immutable request and acknowledges ownership before start reports running. No broker, server or persistent daemon is introduced.
- Generation: each execution/resume records owner PID, start identity and generation. A client reopening the job reads status only. It never restarts work implicitly.
- Status: worker is the sole status/part writer. Publish via existing atomic_json/atomic_bytes. Report state, active unit, counts, last update, source/plan identity and structured errors. Counts refer to committed units.
- Stop: an explicit control operation atomically writes a generation-scoped stop request; read-only viewers do not. Worker stops scheduling, safely finishes/commits or abandons the active unit according to its supported boundary, closes its pool, publishes cancelled/interrupted and releases the lock. Paper shutdown retains the host's separate position-aware rules.
- Recovery: resume first obtains the free OS lock; verify owner exit and all scientific/output identities. A held lock means busy even if the heartbeat is old. A free lock with nonterminal status permits an explicit interrupted-state recovery, not automatic execution.
- Parts: temporary writes are not completion. Validate manifest/checksums and full digest before accepting a committed part; ignore/quarantine incomplete parts into new diagnostic state without rewriting historical evidence. Resume never skips a unit because an unverified file merely exists.
- Repeat: reopening is read-only; starting an already completed compatible job returns its result references. Explicit resume may continue failed/interrupted/cancelled compatible work with a new generation. Changed inputs create a new job.
- Compatibility: structured refusal names changed fields (data, config, source, scenario, policy, required traces/schema). It is not a suffix added to a display string.

Acceptance fixtures cover competing starts, stale heartbeat with a live lock, worker exit, viewer disconnect, interrupted part publication, repeated stop requests, compatible resume and changed-input refusal. Platform-specific process-launch details belong to the implementation task; the ownership protocol is no longer open.

### 7.3 Pinning, session identity and three source policies

P1 reads actual collection.json and sessions/<session-id>.json. Lab.recorded_intervals merges build intervals across short gaps and is coverage only. batch.window_inventory currently sorts window_start_ns and marks later duplicate market rows with keep='first'; tied order is not a declared recording-quality policy. Preserve every candidate source in discovery, then explicitly choose one for a saved selection.

Represent three separate selection fields:

1. **Duplicate-recording choice:** market ID → collection/session/committed-build selection, with alternative recordings and exclusion reasons.
2. **Reference-series policy:** the within-window series, units, availability and semantic provenance (including PolyBolt versus RTDS and assumed settlement/tie/fee rules). Same market ID does not prove series equivalence.
3. **Trading-capture provenance:** original run/capture/import ID and supported reconstruction contract. Import/reconstruct is explicit; a capture is not silently merged into an independent collector archive.

Every selection reopen, worker load and diagnostic explicitly reconstructs Reader with the saved manifests. Cache keys include pinned source identity; an old Lab/cache path that loads latest builds is not acceptable. The same market retains one development/evaluation identity across every recording.

### 7.4 Hashes, read-only access and evaluation exposure

Use the content-hash method and named source-tree discipline of staged.code_identity as the scientific compatibility basis. Put a narrow shared identity helper in workflows/artifacts.py or extract it compatibly; do not import a settlement experiment controller just to hash files.

Include the new workflows package, its CLI adapter, and reused result-affecting strategy_lab/warehouse query helpers in the hashed inputs as well as the existing strategy/runtime/replay/execution/protection trees and scenario content. Canonically enumerate and deduplicate paths, version the digest scheme, and preserve old identities. Git HEAD and dirty state remain displayed provenance, not sufficient job identity. Exact data/config/artifact/schema identities are required in addition to code.

W6 open, inspect, compare, pagination and ordinary saved-trace access are read-only. Do not call explorer study-index construction or timelines.generate/generate_many on these paths. Existing get_trace/load_original/load_cached are read helpers and may be reused only with verified saved identities. A missing trace reports availability; it never triggers a replay or cache write automatically.

Results deliberately stay under var/research/workflows. The existing holdout_untouched scans summaries.parquet under var/research and the legacy timeline cache, but it does not by itself cover every new job manifest/partial format. Integrate new usage records with that guard; do not evade it by moving outputs or changing filenames.

Before an explicit reserved-evaluation or diagnostic generation operation, call frozen_split and holdout_untouched, enforce the selected canonical split/freeze identity and record the accepted exposure in a guard-visible record. Interrupted jobs and partial results must remain visible to later checks even if no final summary exists. Ordinary read operations do not write that record; opening protected outcome views is a separate explicit access operation. The guard records known software use, not all prior human inspection.

Generic candidate save/freeze stores new workflow records. Qualification.freeze and qualification.compare retain their existing P7 semantics and are not generic candidate or comparison owners. A new split/evaluation protocol is outside the initial release.

### 7.5 Paper-profile translation is explicit, complete and validated

profiles.py consumes a resolved candidate and explicit operational inputs. It does not copy a research dictionary into YAML or merge Lab defaults.

A runnable profile requires:

- venue: paper, a complete paper block (starting collateral/account identity, fill/latency settings, supported resolution source and declared simulated redemption behavior);
- state_directory, report_directory and an applicable settlement.ledger_path, isolated and satisfying paper-path validation;
- strategy kind and explicitly chosen parameters with source/provenance, lifecycle.mode and its cutoff/recovery rules, required protection;
- market family/selector, run mode/duration/preparation/evaluation settings, risk/sizing, account/readiness settings, recording choice, and applicable settlement trial/run limits;
- a bounded launch protocol and a translation report stating field origin, omitted simulation assumptions, explicit operational additions and unresolved requirements.

Validate through runtime.run_config.load_trading_run_config plus --paper --validate-config. Unknown/missing operational fields or incompatible assumptions produce a structured block; schema validity is not qualification. Do not silently transfer implicit provisional-K acceptance from RESEARCH_BASE_PARAMETERS. An explicit paper tolerance must be separately selected, reported as a deviation and labeled research-only. Preserve candidate identity and explain any operational change.

P6 must create an ask70 **paper** profile from its actual schema: protection required, lifecycle flatten_before_close, run_mode single_window. The host restricts continuous mode to settlement lifecycle modes; do not copy the settlement hold/continuous block into ask70.

### 7.6 Named evidence and the Step 2 deferral

Historical hold evidence is the S0 chain in the Step 2 execution record:

- Original fill/commit: s0_reach/reports/step2_s0_reach_hold_paper_20261020T124243Z.
- First restart: step2_s0_reach_hold_paper_20261020T124549Z kept the shares but stopped.
- Fixed replacement: step2_s0_reach_hold_paper_20261020T124727Z started against the same account/ledger, tracked the committed hold, rolled and committed another filled window. It did not contain another intentional stop/restart inside that replacement session.
- Exact S0 reconstruction was refused: s0_reach/reconstruction/reconstruction.json lacks raw context. S2-protection's sale-window reconstruction is also open, as are the later accounting findings.

These paths are under 'var/research/trading_integrity/step2/paper/'. They are observations, not accepted complete P5 proof. P5 acceptance requires the follow-up F1–F4 accounting/reconstruction work and a supported complete capture, or a later explicitly narrowed evidence decision. Keep that work deferred as the user requested.

P6's proof is **ask70 host recording plus recording reconstruct**, after a valid isolated paper profile and fixture-level schema/recording checks. No existing ask70 capture or full reconstruction pass is claimed. If it contains host protection orders whose reconstruction depends on deferred Step 2 F3, that part remains gated. Offline contract/fixture work can proceed; do not bypass the dependency by calling the settlement lab.

## 8. Implementation packets: owner and forbidden caller

The planned package is src/tyrex_pm/research/workflows/. Module names below are the initial ownership assignment; combine tiny helpers only when the contract remains clear. New operations are exposed under the proposed tyrex-research workflow namespace; existing operator commands keep their meanings.

| Packet | Planned owner and existing foundation | Must not become the operation | Acceptance / gate |
|---|---|---|---|
| P0 — Contracts/fixtures | workflows/contracts.py and artifacts.py; reuse atomic_json and OS job-lock pattern; source identity discipline | No generic platform, runtime rewrite, editing historical artifacts or treating Git HEAD as the hash | Versioned requests/outputs, job protocol, structured refusals, source-hash coverage and small immutable fixtures. Can start offline. |
| P1 — Library/selections | workflows/data.py; Reader(manifests=...), WindowCatalog, collection.json, sessions/*.json and quality publications | Lab(), keep-first window_inventory as selection, recorded_intervals as sessions | Exact pin passed on every reopen; three source policies; duplicates/changed parts; new publication/compaction and evaluation partition checks. |
| P2 — Experiment/jobs | workflows/experiments.py and jobs.py; settlement schema, explicit load_window/run_window, compatible part/digest conventions | Lab.run, staged.Design/preview, batch.run_batch/_run_group or 35-second fallback as shared operations | Bounded resolved plan; independent locked worker/status; fresh/resume equality; orphan/stop/partial-publication checks. Stable shared interfaces. |
| P3 — Results/candidate | workflows/results.py and profiles.py; saved bundles, pure frames.summary, new candidate records, frozen_split/holdout_untouched, load_trading_run_config | Explorer index/cache writes on read; timelines.generate on open; Qualification.freeze or qualification.compare as generic operations | Read-only reopen/compare, pairing/denominators, guard-visible interrupted use, complete profile translation and reference preservation. |
| P4 — Collection controls | workflows/collection.py; supervisor.Collector/resume_profile, capture.files.Ownership, existing normalization/quality | An invented tyrex-data start/stop command, duplicated supervisor or competing normalizer/quality worker | Controlled start/status/stop/resume of supported profiles; actual session files and saved-profile hash respected. Separate from P1 read-only work. |
| P5 — Paper/reconstruction | workflows/paper.py; tyrex-pm run --paper, watch-run, settlement status, recording reconstruct and host locks | New order path, inferred qualification or automatic repair of deferred Step 2 defects | Named S0 observations linked; full acceptance waits for follow-up evidence. Offline profiles/read adapters can finish first. |
| P6 — ask70 recording | profiles.py/paper.py plus host RecordingConfig; valid ask70 paper/protection/flatten-before-close profile | strategy_lab batch/Lab, live YAML copied as paper, z_gap as an unplanned equal alternative | Fixture contract first; supported capture/reconstruction proof later. No ask70 replay-lab work or unqualified protection replay claim. |

Implementation-plan tasks must name the exact callable request/output, owner, dependencies, meaningful tests and retained evidence. Do not interpret a listed foundation as permission to reuse all of its side effects or defaults.

### Pre-plan acceptance checks

Map these checks into tasks before implementation:

- Pinned source survives a new publication; missing checksum/ambiguous duplicates fail without changing an old selection.
- Job owner disconnect, competing start, stale heartbeat, interrupted atomic publication and explicit resume obey the OS lock.
- ETA is omitted without measured samples; mismatches are structured refusals; limits are validated before grid expansion.
- Open/compare/ordinary timeline access make no filesystem writes. Diagnostic generation is explicit and covered by the holdout guard.
- Interrupted or partial workflow evaluation stays visible to the existing guard; no reserved outcome plot opens incidentally.
- Profile translation supplies all operational fields, does not merge research-only tolerances, and validates ask70's actual lifecycle/protection restrictions.
- Notebook adapters retain equivalent explicitly chosen inputs and view old outputs; changed normalization/defaults are versioned rather than silently rewriting results.
- Known Step 2 accounting/reconstruction limitations remain visible in results, candidate evidence and paper gates.

These are operation-contract tests and migration demonstrations, not tests mirroring field assignments. P0–P4 can proceed without repairing deferred Step 2. P5/P6 paper acceptance remains per capability.

## 9. Decisions fixed by this review

| ID | Topic | Design decision | Remaining implementation proof |
|---|---|---|---|
| D1 | Data-library first increment | Browse and prepare existing recordings first; collection controls are P4 in the same complete release | W1 default and actual source/session fields align |
| D2 | Pinning | Every reopen passes saved committed manifests into Reader; no Lab latest-data path | New publication/compaction and checksum regression |
| D3 | Source identity | Separate duplicate-collection choice, within-window reference policy and trading-capture provenance | No keep-first selection or implicit source stitching |
| D4 | Job ownership | Job directory + OS lock + immutable request + atomic status + stop request + verified parts; independent worker, read-only viewers | Lock/crash/stop/partial/resume tests; no remaining high-level mechanism choice |
| D5 | Results and preview | Saved-bundle reads are pure; metric pairing/denominators explicit; no guessed ETA | Filesystem side-effect checks, missingness and estimate provenance |
| D6 | Candidate/profile | New candidate records and existing holdout guard; complete validated paper translation without implicit research defaults | Translation assertions, guard-visible partial use and unsupported-field refusal |
| D7 | Step 2 boundary | Offline packets proceed; named S0 evidence remains partial; follow-up stays deferred | No paper pass label or hidden execution fix in workflow work |
| D8 | Second plugin | ask70 host recording/reconstruction with a new valid paper profile, required protection and flatten_before_close | Fixture/capture support proven; host-exit reconstruction dependency remains visible |

The review leaves implementation details, not broad architectural questions: choose compatible unit batching, local worker launch mechanics and schema naming within these decisions. An implementation plan should resolve those in bounded tasks with evidence. If a design decision changes, update matching W/D/P references and visibly reconfirm the affected canvas item.

## 10. Completion and Step 4 handoff

Step 3 is complete when:

- W1–W8 have implemented shared inputs/outputs, errors, ownership and repeat/resume rules.
- A small settlement journey completes fresh and after research interruption with equal logical outputs under compatible inputs.
- Selection pinning survives new data; ambiguity, changed parts and incompatible resume are exposed.
- Results reopen without a kernel; comparison explains differences and preserves unknown outcomes.
- Candidate translation produces a validated paper profile without overwriting references.
- Relevant Step 2 evidence is accepted and the paper/reconstruction demonstration retained. Optional unqualified capabilities remain explicit.
- Existing collection controls avoid duplicate supervision; ask70 demonstrates the shared host-recording/reconstruction contract within its supported paper lifecycle.
- CLI/notebook clients share operations; no copied strategy/execution implementation or live submit control is introduced.
- Operation and view contracts are reviewed before Step 4 binds an interface to them.

Accept independent packets as they finish. Offline acceptance alone is not full Step 3 completion. Step 4 sketches may proceed from these contracts without adding another backend.

The Data Library journey in section 11 is an additional completion check: warehouse inspection, one data recipe, a reviewed preview, an exact window set, and an experiment opened from that selection id. The warehouse view may be connected when increment DL-1 passes. Paper launch, a live recording session, and ask70 capture remain gated and do not block browsing existing recordings. The implementation order is [step_03_data_library_backlog.md](step_03_data_library_backlog.md).

## 11. Data Library extension — 21 October 2026

Agreed journey:

**Warehouse → data scenario → scope and composition → selection preview → exact window set → experiment opened by selection id.**

The user states a research question, such as settlement configurations on BTC five-minute windows for selected dates where both books and the required reference data are available. The user does not type paths, manifests, or worker units. CLI, notebook, and frontend call the same operations and receive `workflow_result_v1`.

This section does not rebuild results, strategies, notebooks, or the trading host. It completes the P1 discovery and pin path, the P2 handoff from a selection id, and preparation orchestration. P4's live recording-session proof, P5 launch, and P6 capture stay on their existing gates.

### 11.1 Objects

| Object | Identity | Changes |
|---|---|---|
| Data recipe | `workflow_data_recipe_v1`: question, version, scope, rules, intended use, label policy, partition, limitations | A saved version is immutable. Reevaluation can match newly recorded windows. |
| Selection preview | `workflow_selection_preview_v1`: `preview_id` over the recipe version, scope, rules, and the inventory fingerprint of the manifests consulted | Valid only for that fingerprint. Preparation or a new publication requires a new preview. |
| Window set | Existing saved selection, extended with real manifests, checksums, source choices, policies, and warm-up dependencies | Immutable. Legacy records with the empty `workflow_pin` manifest stay readable and cannot be bound. |
| Execution scenario | Existing scenario file and experiment `scenarios` field | Unchanged meaning. A data recipe must not be stored in this field. |
| Experiment | Selection id plus strategy, execution scenarios, baseline, metric definitions, acceptance criteria, and `max_units` | Server-built replay units. The request does not contain those units or reconstructed pins. |
| Preparation job | Separate from an experiment job. Kind is `normalize`, `quality`, or `verify` | Durable progress. It does not create a window set or a recording session. |

### 11.2 Read-only warehouse discovery

Owners: `Reader`, `WindowCatalog`, committed manifests, and published quality assessments. No new storage engine. A disposable overview index is allowed only through an explicit rebuild operation. A read must not build it.

Operations:

| Operation | Request | Result |
|---|---|---|
| `library.windows` | Configured `roots`; optional collection, session, asset, duration; `start_utc` and `end_utc`; `limit` and `cursor` | One page of windows, bounds, freshness, unavailable roots, `next_cursor` |
| `library.coverage` | The same scope plus `bucket_s` | Buckets of counts and denominators. A bucket is a summary, not proof of complete coverage |
| `library.window` | Warehouse, collection, and `market_id` | Intervals, duplicates, reference choices, label provenance, warm-up requirements, evidence links, and the same tracks as the list |

Time rules for the first increment:

- Storage and filter bounds are UTC.
- The interval is half-open on window start: `start_utc <= window_start < end_utc`.
- Warm-up and other context required to replay a selected window are dependencies of that window. They do not pull an earlier window into the page.
- Missing expected windows are inferred only when the request sets `schedule: btc_updown_5m`. Otherwise absence is not invented.

Each window reports seven tracks: `recording`, `normalization`, `up_book`, `down_book`, `reference`, `quality`, `outcome`. Each track is one of `present`, `missing`, `not_expected`, `unassessed`, `unknown`, `withheld`, or `unavailable`.

- `present` and `missing` require an assessment that looked.
- `unassessed` means no assessment has run.
- `unknown` means the assessment could not classify the value.
- `unavailable` means the root, collection, or source could not be read.
- `withheld` means a value exists and the partition forbids returning it.
- `not_expected` means the track does not apply to that slot.

Map tracks from existing datasets: raw manifests, committed normalized builds, `book_updates` for each leg, the reference series named by policy, quality publications, and `resolution_observations`. Do not collapse those datasets into one Eligible pill.

Every inventory result states `timezone: UTC`, the half-open window-start rule, `bucket_s` when aggregated, `aggregation: count_of_windows_by_start`, publication ids, and `assessed_at`. Unknown counts are a separate number. An unknown or unavailable source is not a zero and is not complete.

`library.collections` remains the collection and session list. `library.inspect` remains inspection of a saved selection. Neither operation becomes the warehouse inventory.

Reads do not publish quality, start normalization, create `var/research/workflows/`, or rebuild an index. The current `call()` creates the workflow root before every operation. DL-1 removes that side effect for these reads. A missing workflow root is an empty research record, not a reason to create one.

**DL-1 acceptance:** from a fresh process, list, coverage, and detail agree on bounds, duplicates, timezone, and assessment identity for one complete window and one incomplete window. Counts of unknown are not reported as zero. The archive and workflow directories are unchanged. No collector is started.

### 11.3 Real selections

`selection.save` currently stores `_pin_manifest()` with `build_id: workflow_pin` and `files: []`, plus any caller-supplied `pin_parts`. `selection.open` skips the membership check when the pinned catalog is empty. That pair does not preserve a warehouse window.

A window set saved from a preview stores:

- the included market ids and the collection, session, and build chosen for each;
- the committed manifest documents and sha256 of each pinned part, passed back to `Reader(..., manifests=saved)`;
- duplicate-recording choice, reference-series policy, and trading-capture provenance as separate fields;
- label policy version and partition snapshot;
- warm-up and context bounds required by the loader, including the prior reference or clock observation the loader already requires;
- `preview_id` and the inventory fingerprint that preview used.

Reopen verifies checksums before serving rows. A changed or missing part returns `pin_mismatch` and does not substitute the latest build. If `included` is non-empty and the pinned catalog has no windows, the result is `empty_catalog`, not success.

Records already saved with the empty `workflow_pin` manifest open with `limitation: legacy_unpinned`. They are not rewritten. `experiment.bind` refuses them.

The three source policies in section 7.3 stay in force. Multiple sessions or builds of one collection are an explicit choice, not keep-first.

**DL-2 pin acceptance:** a fresh process reopens the same windows after a later publication beside the pin. The old manifest checksums still match. A changed pinned part is refused. An empty pinned catalog with expected members is refused.

### 11.4 Recipe resolution and preview

Initial catalog, versioned and editable before save:

| Recipe | Rule |
|---|---|
| `eligible_windows` | Scope windows whose required books, reference, and warm-up are `present`, subject to the label policy and partition |
| `custom_window_set` | The same scope plus the filter object below. `selection_mode: all_matches` includes every window that passes. `selection_mode: picked` intersects those matches with explicit market ids. A pick outside the scope or the filters is excluded; it cannot bypass a requirement or the partition guard |
| `missing_inputs` | Scope windows with a required track `missing` or `unassessed`. Diagnostic only. Ordinary experiment binding refuses this recipe |

The prototype's internal key `manual` is not a schema name. `custom_window_set` replaces the earlier `manual_cases` meaning of a bare id list.

Filter object, combined with AND. Date bounds stay UTC and half-open on window start.

| Field | Values | Rule |
|---|---|---|
| `outcome` | `any`, `up`, `down`, `unresolved`, `unknown` | Market outcome, not the strategy position or its profit. `up`/`down` require a classified winner. `unresolved` means no resolved observation. `unknown` means a resolved or conflicting observation that cannot be classified. These are not the same state |
| `label_source` | `any`, `provider_confirmed`, `proxy`, `unknown` | Provenance of that outcome. `proxy` is the research label from `proxy_outcome` (`nonzero_payout_entry_index_v1`), not a qualified settlement. `provider_confirmed` requires an attested resolution or boundary. An archive that only has proxies supports the filter and may match nothing; that zero is not `unsupported_measurement` |
| `daily_time` | enabled flag, `start`/`end` as `HH:mm`, IANA `time_zone` | Half-open on the zoned window start. The first supported zones are `UTC` and `Europe/Paris`. The server validates the zone; it does not read the browser clock. If start is after end, the window matches `[start, midnight)` or `[midnight, end)` on that zoned day. Equal endpoints are `invalid_filter`. Disabled means all hours |
| `weekdays` | ISO weekday numbers, Monday = 1 | Interpreted in `time_zone`, including the daylight-saving offset of that window start. An empty set is `invalid_filter` |
| `selection_mode` | `all_matches`, `picked` | `picked` also carries the market ids. Ids that fail the filters stay stored on the draft and are excluded with a reason |

The partition guard runs before any outcome or label-source value is read, counted, or shown. A reserved market is `withheld` and is absent from outcome-mix counts. Input requirements — both books, reference series, warm-up, and a known scoring outcome — are a separate layer checked after these filters. Duplicate-recording and reference-series choices stay explicit.

`volatility`, `spread`, `liquidity`, and `reversal` return `unsupported_measurement` and name the missing definition. Reversal is not inferred from the final Up/Down outcome. A supported filter declares field id, value type, units, data dependency, measurement version, and whether it is retrospective. No matches from a supported filter is a different result from an unsupported filter.

`data_recipe.list` returns the catalog and applicability reasons. `data_recipe.resolve` returns candidates that match the filters and the final included and excluded rows after requirements. `preview_id` covers the canonical filters, time semantics, scope, requirements, source choices, and assessment identity.

The saved window set stores exact membership and the versioned filter object, selection mode, and recipe id. `selection.save` requires `preview_id`. A mismatch returns `preview_stale` and writes nothing. Opening the set returns those filters. Applying them again is a new resolve and a new set; it does not change the saved membership.

An outcome or label-source filter other than `any` marks the set `population: outcome_conditioned` and `intended_use: diagnostic`. That set may be bound to an otherwise supported experiment. Results and comparisons keep the limitation. It is not a representative sample. A `missing_inputs` set does not become replayable because its filters match.

**DL-2 preview acceptance:** daytime and overnight bounds, UTC and `Europe/Paris` hours, weekday boundaries, and a daylight-saving transition match the server clock rules. Up, Down, unresolved, and unknown stay distinct, as do provider-confirmed and proxy. Reserved markets do not contribute to filter decisions or outcome counts. All-matches and picked mode produce the declared population. The saved set reopens with the same membership, filters, time zone, and sources. A copied filter definition produces a new preview id. CLI and notebook receive the same included and excluded ids.

### 11.5 Experiment binding

`experiment.bind` accepts a selection id, strategy kind and parameters or a bounded configuration list, execution-scenario identities, baseline id, metric definitions, acceptance criteria, and `max_units`. It does not accept `units` or `pin_parts`.

The operation loads the window set, refuses `legacy_unpinned`, `empty_catalog`, and `pin_mismatch`, and checks the settlement schema. It also refuses `missing_inputs` with `diagnostic_not_replayable`. An `outcome_conditioned` set is allowed and the experiment records that population limitation. Only `settlement_value` is a workflow replay adapter. ask70 remains the host-recording profile from P6, not a replay unit kind. Research defaults are refused. The unit count is `windows × configurations × execution scenarios` and is refused before expansion when it exceeds `max_units` (default 64).

Server-built units are `strategy_replay` rows carrying the pinned manifests, scenario path, and parameters. `job.start` runs those units. The existing caller-supplied `experiment.save` path stays available for fixtures and is marked `units_source: caller`. A Data Library experiment uses `units_source: selection`.

Each metric records name, unit, denominator, eligible population, and missing-value rule before any result is opened. Unknown P&L is not zero. Acceptance criteria are stored on the experiment and are not inferred from a later chart.

**DL-3 acceptance:** one small recorded selection, already normalized, reaches `strategy_replay.run_window` with no units in the request. A fresh process reopens the same plan digest. CLI and notebook resolve the same unit count and the same pinned manifest checksums.

### 11.6 Managed preparation

`library.prepare` stays the synchronous delegate used by current tests. New work uses a preparation job so a browser request is not the worker.

| Operation | Behavior |
|---|---|
| `preparation.plan` | Names the owner, the lock roles, whether the work is collection-wide, and `safe_stop` |
| `preparation.start` | Starts one owner after the plan id is still valid |
| `preparation.status` | Reads durable status. It does not start work |
| `preparation.stop` | Writes a generation-scoped stop request |

Owners stay `normalize_once`, `publish_quality`, and `verify`. Verify reads and must not publish quality or write normalized builds. Normalize and quality publication are collection-wide even when the recipe's date range is smaller. The plan says so.

Locks: `Ownership` roles `normalizer`, `quality-publisher`, and `quality-index`, plus the collector role `collector`. If any relevant role is held, the result is `owner_busy`. A running collector is busy even when the normalizer lock is briefly free. Do not start a second writer.

`safe_stop` is `segment_or_build` only after the existing owner loop observes the stop file at a segment or build boundary. Until that observation exists, the plan reports `safe_stop: unsupported` and stop does not pretend to pause inside a build. There is no per-window pause or resume. Compatible restart, after the lock is free and the same warehouse and collection identity still match, continues from committed owner outputs. Raw observations are not rewritten.

Worker completion is not market eligibility and not qualification. The user refreshes the selection preview; the preparation job does not silently update a window set.

**DL-4 acceptance:** an isolated incomplete fixture archive gains the derived output, raw observations are unchanged, a held owner lock refuses a second start, and an interrupted publication restarts from committed owner work. No collector is launched.

### 11.7 Interface transport

The frontend, CLI, and notebook call `tyrex_pm.research.workflows.call`. The envelope and error `{code, reason, fields}` are the only business result. A later local HTTP adapter may wrap that function; it must not add rules.

The browser sends intent and ids. It does not assemble manifests, pins, or replay units. Reads stay free of workflow-directory creation, publication, and index rebuilds. Preparation start, window-set save, experiment launch, collection start, and evaluation open remain explicit.

The review prototype under `prototypes/ihm-review/` is not this contract. Bind a warehouse view only after DL-1. Bind recipe, save, and experiment actions only after DL-2 and DL-3. Bind preparation controls only after DL-4.

### 11.8 Evaluation partitions and labels

Partition membership is the market id across every collection and capture of that market. A second recording does not make a reserved market developable.

For `intended_use: evaluation`, inventory, preview, counts, labels, and timeline colors pass through the existing holdout guard before outcome values are returned. A reserved market may appear as `partition: reserved` and `outcome: withheld`. Its resolution value, example row, and outcome color are omitted. Development use may list the same disposition and still must not return the reserved outcome.

Retrospective labels and after-close fields are not decision-time inputs and are not written into the pinned causal series.

Label classes stay distinct: `provider_confirmed`, `proxy`, and `unknown`. A proxy is not provider-confirmed. Unknown is not a loss and is not zero.

### 11.9 Decisions

These are the defaults for the backlog. Reopen one only by editing this section.

| Topic | Decision |
|---|---|
| Operation names | Add `library.windows`, `library.coverage`, `library.window`, `data_recipe.list`, `data_recipe.resolve`, `experiment.bind`, and `preparation.plan/start/status/stop`. Do not overload `library.prepare` or caller-unit `experiment.save`. |
| Date filter | UTC, half-open on window start. Warm-up is a dependency, not a page filter. |
| Schedule gaps | Infer unrecorded slots only for an explicit `btc_updown_5m` schedule. |
| Preview binding | Save requires `preview_id` and refuses `preview_stale`. |
| Legacy pins | Empty `workflow_pin` manifests open as `legacy_unpinned` and cannot be bound. No bulk migration. |
| Empty catalog | Non-empty membership with no pinned windows is `empty_catalog`. |
| Collector ownership | Collector lock or the owner role lock means `owner_busy`. |
| Stop granularity | No per-window pause. `safe_stop: unsupported` until the owner observes a stop at a segment or build boundary. |
| Transport | In-process `call` is the first frontend transport. |
| Read side effects | Inventory reads must not create the workflow root. |

### 11.10 Increments

| Id | Depends on | Delivers | Connect the interface when |
|---|---|---|---|
| DL-1 | Current Reader and WindowCatalog | Read-only windows, coverage, and detail | Warehouse view only |
| DL-2 | DL-1 | Real pins, three recipes, preview id, save checks the preview | Recipe and window-set actions |
| DL-3 | DL-2 and the existing replay worker | `experiment.bind` to `strategy_replay` units | Experiment open from a selection id |
| DL-4 | Existing owners and job lock | Preparation plan, progress, busy owner, safe stop as actually supported | Preparation controls |
| DL-5 | The increment being bound | CLI, notebook, and frontend share that increment's results | That screen only |

DL-1 plus DL-2 plus DL-3 on windows that are already `present` is the smallest slice that reaches replay. DL-4 is required only when the chosen windows still need processing. P4 live session proof, P5 launch, and P6 capture are not dependencies of DL-1.

Acceptance shared by the later increments:

- Timeline, list, and detail agree, and reads add, modify, and delete nothing in the archive or workflow root.
- A fresh process reopens real pinned inputs after a later publication; missing or changed inputs are refused.
- The saved set matches the reviewed preview, and duplicate or reference choices are explicit.
- A second preparation owner is refused; interruption and restart follow the declared `safe_stop`.
- A small recorded selection reaches replay without manually supplied units.
- CLI, notebook, and frontend show the same plan. Holdout outcomes do not appear in filters, counts, labels, or previews.

