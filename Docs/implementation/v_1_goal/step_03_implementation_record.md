# Step 3 implementation record — reusable workflows

Status: offline operations and the read-only paper/profile slice are implemented and covered by local tests. This is not full Step 3 acceptance. P5 launch/restart and P6 public-feed capture/reconstruction stay gated. The Step 2 follow-up stays deferred.

## Workflow

Public entry points are `tyrex_pm.research.workflows.call` and `python -m tyrex_pm.research.cli workflow <operation> --request <file> --root <path>`. Both return the `workflow_result_v1` envelope (`operation`, `ok`, `result`, `error`). Diagnostic logs stay off that stdout stream. Notebook helpers in `workflows/notebook.py` call the same operations and may build a frame afterward.

Journey, operation, and the check that covers it:

| Step | Operation | Evidence |
|---|---|---|
| Library | `library.collections` | Lists `collection.json` and `sessions/*.json`. Coverage spans are not treated as sessions. |
| Preparation | `library.prepare` | `normalize`, `quality`, or `verify` only. `kind=collect` is refused. A held normalizer `Ownership` lock becomes `preparation_failed`. |
| Pinned selection | `selection.resolve`, `selection.save`, `selection.open` | Duplicate collections, reference-series choices, and trading-capture provenance are separate. Reopen checks pinned sha256 and passes saved manifests to `Reader`. A sibling file does not change a saved selection. |
| Experiment | `experiment.resolve`, `experiment.save` | Settlement schema load. Research defaults are refused. `max_units` is applied before grid expansion (default 64). |
| Preview | `experiment.preview` | Completed parts must match the part manifest. Estimate is `null` with `no_measured_sample` when no duration was measured. |
| Job | `job.start`, `job.status`, `job.stop`, `job.resume` | Detached `python -m tyrex_pm.research.workflows.worker`. OS lock `.workflow_job.lock`. Immutable `job.json`. Atomic `status.json`. Generation-scoped `stop.json`. |
| Results | `results.open`, `results.compare`, `results.trace` | Reads do not add, modify, or delete files other than an already growing worker log, which the snapshot ignores. A missing trace returns `available: false`. |
| Explicit writes | `comparison.save`, `diagnostic.start` | Separate from reads. Diagnostics write a guard-visible usage record before any replay. |
| Candidate | `candidate.save`, `candidate.freeze` | New workflow records. `Qualification.freeze` is not the owner. |
| Evaluation | `evaluation.open` | Requires `purpose=evaluation`. `holdout_untouched` scans `workflow_usage.json` under the research root. A second open of the same holdout markets returns `holdout_exposed`. Competing guard access returns `guard_busy`. |
| Paper profile | `profile.generate`, `profile.validate` | Chosen strategy parameters are copied. Operational fields are explicit. Output directory name must be `generated`. YAML is checked with `load_trading_run_config`. Research defaults and overwrites are refused. `qualified` stays false. |
| Collection control | `collection.start`, `collection.status`, `collection.stop`, `collection.resume` | Stop writes `sessions/<session>.collector-stop` only when session and generation match. `Collector.run` polls that file. `launch` is not set by the tests. |
| Paper observation | `paper.preflight`, `paper.status` | `launch_allowed` is false. Blocker `step2_follow_up_open`. S0 report names are observations with `accepted: false`. Preflight does not change files. |

## Fresh-process example

From the repository root, a new Python process can resolve, run, and read the closed-loop fixture:

```text
python -m tyrex_pm.research.cli workflow experiment.save --request request.json --root <workflow-root>
python -m tyrex_pm.research.cli workflow job.start --request request.json --root <workflow-root>
python -m tyrex_pm.research.cli workflow job.status --request request.json --root <workflow-root>
python -m tyrex_pm.research.cli workflow results.open --request request.json --root <workflow-root>
```

The save request used one unit, `{"id": "replay", "kind": "replay_engine", "market_id": "m1"}`. A fresh `results.open` process returned `workflow_result_v1` with `fill_price` `0.55`, `fill_at_ns` `310000000`, `engine` `replay.engine.Engine`, and `realized_pnl` null. Stdout was only the envelope.

## Interruption, resume, and CLI/notebook equivalence

`tests/unit/research/test_workflow_jobs.py` drives real local worker processes:

- The spawning call returns while the worker still holds the lock, so the parent is not the worker.
- A second `job.start` for that id returns `job_busy` while the lock is held, including when `heartbeat_at` is 0. Heartbeat age does not take over.
- `job.stop` records the current generation. The worker publishes `interrupted`, releases the lock, and keeps the verified first part. Resume uses the next generation, ignores the old stop file, and the logical window rows match a fresh run of the same units.
- Killing the worker releases the lock. Resume then refuses a changed pin (`pin_mismatch`) and a changed source fingerprint (`identity_mismatch`).
- `crash_once` leaves `parts/<unit>.json.tmp` and does not publish the part. Resume quarantines that partial file, commits the unit, and `results.open` returns the same logical value as a completed digest unit (`value` 4) plus `schema` `workflow_part_v1`.

`tests/unit/research/test_workflows.py::test_cli_and_notebook_share_the_replay_result` runs the real `Engine` fixture directly, through a detached worker, through `notebook.open_results`, and through a fresh `tyrex_pm.research.cli` process. Fill price and fill time match. CLI stderr is empty.

## Operation catalog for Step 4

`tyrex_pm.research.workflows.CATALOG` is the binding list. Helper functions such as `execute_unit` are not in it.

`library.collections`, `library.inspect`, `library.prepare`, `selection.resolve`, `selection.save`, `selection.open`, `experiment.resolve`, `experiment.save`, `experiment.preview`, `job.start`, `job.status`, `job.stop`, `job.resume`, `results.open`, `results.compare`, `results.trace`, `comparison.save`, `diagnostic.start`, `candidate.save`, `candidate.freeze`, `evaluation.open`, `profile.generate`, `profile.validate`, `collection.start`, `collection.status`, `collection.stop`, `collection.resume`, `paper.preflight`, `paper.status`.

List results include `next_cursor` when a page is requested (`limit`, `cursor`; default limit 100). Progress for a job is `jobs/<id>/status.json`, not a second result type.

## Packet status

The rows below record the 20 October 2026 checks. They are not acceptance of the Data Library journey in section 11 of [03_reusable_workflows.md](03_reusable_workflows.md). Gaps found by inspecting the implementation on 21 October 2026 are listed after this table and are not new test results.

| Packet | Status | What was checked | Still gated |
|---|---|---|---|
| P0 contracts, catalog, fingerprint, job lock | Verified | Envelope, catalog equality, `workflow-source-v1` content fingerprint, OS lock filename | — |
| P1 library, preparation, pinned selections | Verified | Prepare versus collect, normalizer lock, three source policies, pin mismatch, sibling publication left the saved selection unchanged | No live-warehouse compaction run |
| P2 experiments, preview, detached worker, resume | Verified | Resource limit before expansion, no invented ETA, subprocess lock/stop/kill/crash/resume equality | — |
| P3 results, guard, candidate, profile, notebook | Verified | Read-only snapshot, missing trace, holdout usage consumed by `holdout_untouched`, settlement and ask70 YAML via `load_trading_run_config`, CLI/notebook fill equality | — |
| P4 collection start/status/stop/resume | Implemented; stop contract verified | `collector_stop_matches`, wrong generation returns `stop_target`, matching stop writes the session file. Existing collector shutdown test still passes | No collector process was started |
| P5 read-only paper | Verified | Preflight snapshot unchanged, `launch_allowed` false, S0 observations not accepted | `paper.start` / stop / restart |
| P6 ask70 profile and fixture | Verified for the profile | Protection, `flatten_before_close`, and `single_window` required. Generated YAML validates. Continuous run mode is refused | Public-feed capture and reconstruction |
| Step 2 follow-up | Deferred | Named only as blocker `step2_follow_up_open` | F0–F4 accounting and reconstruction |
| Full Step 3 (spec section 10) | Not accepted | Offline and interface contracts above | Accepted paper evidence, a real collection control session, ask70 capture |

## Code and tests

- Package: `src/tyrex_pm/research/workflows/`
- CLI: `src/tyrex_pm/research/cli.py` (`workflow` subcommand; `replay` unchanged)
- Guard scan: `src/tyrex_pm/research/strategy_lab/qualification.py` (`workflow_usage.json` under the given research root)
- Collector stop poll: `src/tyrex_pm/data_warehouse/supervisor.py`
- Tests: `tests/unit/research/test_workflows.py`, `tests/unit/research/test_workflow_jobs.py`
- Command that passed (19 tests): `pytest tests/unit/research/test_workflows.py tests/unit/research/test_workflow_jobs.py tests/unit/research/test_qualification.py tests/unit/data_warehouse/test_transport.py::test_supervisor_shutdown_and_pending_projection_without_network`

W5 rule in the proposal canvas: a stale heartbeat prompts inspection; a held OS lock stays busy; heartbeat age never authorizes a second writer.

## Interface readiness, separate from acceptance

Step 4 can bind the catalog above for the operations those rows actually cover. Each call is one JSON envelope. Job progress is the worker-owned status file. Screens that launch paper, restart a host, or capture an ask70 public feed do not have an operation yet. Those screens stay disabled while `step2_follow_up_open` is present.

The Data Library screens do not bind to this catalog yet. Warehouse discovery, real pins, recipe preview, and selection-to-experiment binding are specified in section 11 and ordered in [step_03_data_library_backlog.md](step_03_data_library_backlog.md). The warehouse view waits for DL-1. Paper launch, recording-session proof, and ask70 capture remain gated and do not block that view.

## Data Library gaps identified 21 October 2026

Source inspection of `workflows/data.py`, `workflows/experiments.py`, and `workflows/jobs.py`. No new tests were run for this note. The historical evidence above stands.

### Completed foundations that this journey reuses

- `workflow_result_v1`, the operation catalog, and CLI stdout limited to that envelope.
- Collection and session listing, with coverage spans excluded from sessions.
- `library.prepare` refuses `kind=collect` and returns `preparation_failed` when the normalizer `Ownership` lock is held. The call is synchronous.
- `selection.resolve` keeps duplicate-collection choice, reference-series policy, and trading-capture provenance apart.
- Reopen checks sha256 of caller-supplied `pin_parts` and passes the saved manifest list to `Reader`. A sibling file beside a pin does not change the saved selection.
- Experiment schema checks, research-default refusal, `max_units` before grid expansion, and preview without an invented duration.
- Detached worker, OS lock, generation-scoped stop, killed-worker refusal, interrupted publication, and resume equality on fixture units.
- Read-only result open, holdout usage records consumed by `holdout_untouched`, and settlement replay through `Engine` for a fixture unit.
- Paper preflight does not mutate files and reports `step2_follow_up_open`.

### Gaps

| Gap | What the code does | What section 11 requires |
|---|---|---|
| Warehouse discovery | `library.collections` lists collections and sessions. `library.inspect` reads a saved selection. | Paged `library.windows`, `library.coverage`, and `library.window` over committed manifests, with declared bounds, freshness, and unknown counts. |
| Read side effects | `call()` creates the workflow root before every operation. | Inventory reads create no workflow directory, publish nothing, and rebuild no index. |
| Pins | `selection.save` always stores `_pin_manifest()` with `files: []` and `build_id: workflow_pin`. Real checksums are only the caller-supplied `pin_parts`. | The saved manifest lists the committed files and checksums, source choices, policies, and warm-up dependencies. |
| Empty catalog | `selection.open` applies the membership check only when the catalog list is non-empty. | A non-empty window set whose pinned catalog is empty returns `empty_catalog`. |
| Recipes and preview | No data-recipe catalog or preview id. | Eligible, manual, and missing-input recipes. Save accepts only the reviewed `preview_id`. |
| Experiment handoff | `experiment.save` stores caller `units`. `job.start` returns `empty_plan` when that list is empty. | `experiment.bind` builds `strategy_replay` units from the selection id. The request has no units and no pins. |
| Preparation | `library.prepare` runs `normalize_once`, `publish_quality`, or `verify` in the request. | A durable preparation job, collection-wide scope disclosed, `owner_busy` when the collector or owner lock is held, and `safe_stop` only as the owner actually supports. |
| Journey test | Fixture units and an in-memory window list. | A recorded selection reaches replay without manually supplied units, then reopens after a later publication. |

### Deferred gates, unchanged

| Gate | Status |
|---|---|
| P4 live collection start, status, and resume against a real session | Still not run. Stop-file targeting remains the checked part. |
| P5 paper launch, stop, and restart | Gated on the Step 2 follow-up. |
| P6 ask70 public-feed capture and reconstruction | Gated. The paper profile fixture stands. |
| Step 2 follow-up F0–F4 | Deferred. Blocker code `step2_follow_up_open`. |
| Section 11 increments DL-1 through DL-5 | Specified on 21 October, including the custom-window-set filter amendment. Implementation evidence is in the next section. Not accepted as a complete Data Library or as full Step 3. |

## Data Library implementation, 21 October 2026

The gap table above is the inspection from before this implementation. The operations now exist. They are not a claim that section 11 or Step 3 is accepted.

Code: `workflows/inventory.py`, `workflows/recipes.py`, `workflows/preparation.py`, `workflows/preparation_worker.py`, `research/ihm.py`, and `frontend/`. The prototype in `prototypes/ihm-review/` was left as the synthetic reference. The integrated chart imports it, and Vite aliases `react` and `react-dom` to `frontend/node_modules` so the prototype's own React copy is not used.

`python -m pytest tests/unit/research/test_data_library.py tests/unit/research/test_workflows.py -q` — 17 passed in 9.99s.

Fixture checks in `tests/unit/research/test_data_library.py`:

- Inventory list and detail agree, and the read does not create the workflow root.
- Schedule slots appear only for `schedule: btc_updown_5m`.
- Overnight, Europe/Paris, and DST weekdays use the zoned window start. A Sunday-only filter keeps the Sunday start and drops the later weekday.
- A reserved market is `withheld` and is absent from the outcome mix.
- `reversal` returns `unsupported_measurement` with a null result.
- `provider_confirmed` can match zero windows and stays a supported filter.
- A stale `preview_id` writes nothing. A matching preview saves real manifest paths and file sha256 pins. A later sibling publication does not change the reopened set. Changed bytes return `pin_mismatch`.
- `legacy_unpinned` selections still open, with that limitation, and `experiment.bind` refuses them.
- `missing_inputs` binds as `diagnostic_not_replayable`.
- An outcome-conditioned custom set binds `strategy_replay` units, with manifests, `units_source: selection`, and limitation `outcome_conditioned`. The request does not supply units.
- Preparation on an isolated raw archive refuses a held collector lock (`owner_busy`), leaves raw bytes unchanged, commits builds, and a second run reports `new_builds: 0`. Completion does not update the saved selection or grant qualification. Stop before the owner does not run verify. `safe_stop` is `unsupported`.

Live warehouse read, `var/warehouse_btc5m`, collection `380e44875c8e483aa4f76e17cab06c02`, UTC window start `[2026-10-06, 2026-10-07)`:

- `library.windows` returned 114 BTC 300-second windows. The first is `polymarket:condition:0xd6641d895c541efe67c0231ce1a1f0806d6a0df2ae103f043fca8c47f62ba349` at `2026-10-06 14:30Z`, outcome Up, label source proxy.
- The same window's inspector agreed: recording `present` (`sealed_raw_manifest`), normalization `present` (`committed_build`), Up book `missing` (`GAP`), Down book `missing` (`GAP`), reference `missing` (`GAP`), quality `present` (`UNSUPPORTED`), outcome `present`, warm-up `missing` (`GAP`). Availability is a coverage gap. Book-update presence was not treated as coverage.
- A custom set for Up, proxy, Europe/Paris, and weekdays without Wednesday returned 27 matches and included 0. The mix was `{"up": 27, "down": 0}`. Save returned `the reviewed preview includes no windows` and wrote no selection.
- Missing-input recipe for the same dates saved selection `fe35312b19df44a8bcfa2eebfb0580a2` with 114 windows, 1743 manifests, and 6237 file pins. Reopen returned the same id and the stored filters (`weekdays: null` because this recipe does not apply the custom clock). The experiment action stays disabled, with the text that missing-input diagnostics are refused for ordinary replay.

No eligible window in that response has Up book, Down book, reference, and warm-up all `present`, so no live selection was given to `strategy_replay.run_window`. Preparation was not started against `var/warehouse_btc5m`. Paper launch and a new recording were not started.

Integrated UI at `http://127.0.0.1:4173/`: the warehouse page showed those 114 windows and the coverage heatmap; Research states that demo charts are not loaded; Runs keeps launch disabled; Evidence lists the closed gates. A first render crashed with `Cannot read properties of null (reading 'useRef')` because the chart resolved the prototype's React. The alias rebuild fixed it, and the same load then rendered.

## Assessment states and paging, 21 October 2026

`python -m pytest tests/unit/research/test_data_library.py tests/unit/research/test_workflows.py -q` — 20 passed. The newer `assessed_at` wins when one market has several quality folders. `semantic_readiness: UNSUPPORTED` is quality `missing` / `semantic_unsupported`. Partial book coverage is `book_partial` and is included only when `requirements.coverage` is `partial`, with the usable ratio stored. A publication report with `reason: window_not_started` is excluded as `window_not_started` and `preparation_can_help` is false.

Live archive, same collection, UTC `[2026-10-06, 2026-10-08)`, after the workspace server loaded this code:

- `library.windows` with `limit: 25` returned 25 of `matched: 342` and `next_cursor: 25`. The page starts at `2026-10-06 14:30Z`. The next page starts at `16:35Z`.
- Hourly `library.coverage` over that range: 30 buckets, 342 windows, 0 inputs ready, 0 preparation, 342 coverage gap.
- Quality: 280 `unknown` / `window_not_started`, 62 `missing` / `semantic_unsupported`. Up book: 280 `window_not_started`, 61 `partial_coverage`, 1 `GAP` with usable duration 0.
- Complete coverage included 0 windows (61 partial, 1 absent, 280 not started). Partial coverage included 61 and stored usable ratios. The empty complete population was not saved.
- Saved set `a623f7b6d3cc4525b5310d3613072020`, name `BTC 5m partial book coverage, 6-8 Oct`, recipe `eligible_windows`, `requirements.coverage: partial`, 61 windows. The first stored Up-book ratio is about 0.697. This is the partial population, not an inputs-ready set. The earlier missing-input set `fe35312b19df44a8bcfa2eebfb0580a2` is unchanged.
- `preparation.plan` / `preparation.start` kind `quality`, id `quality-20261021`, completed without changing the selection and without granting qualification. `owner_result.publication_id` is `31b103ed2b29721474c0d98c995f2330c365acb41f21a87934ed47db198fd072`, the publication that already existed, `markets: 342`. It did not reassess the 280 windows. Those windows start after the capture horizon `2026-10-06T19:36:34Z` (`last_received_at`). The publisher records them as `window_not_started`. Preparation cannot create the missing capture. `safe_stop` remains `unsupported`.

The integrated page at `http://127.0.0.1:4173/` shows 25 windows per page, Previous/Next, a page-size choice of 25 or 50, and an hourly chart for the date range rather than the visible page. Eligible windows hides outcome, label, hour, and weekday controls. Continue stays on Scope until the preview returns, then Composition shows the grouped exclusions, including `2026-10-06 14:30 · book_absent · Up book 0s / 300s`. Switching to partial coverage clears that preview; Refresh then shows `61 windows in this population · 281 excluded`. No order, paper launch, or new recording was started.

## Recordings and exploratory replay, 21 October 2026

`python -m pytest tests/unit/research/test_data_library.py tests/unit/research/test_workflows.py -q` — 22 passed. `ruff check` on the touched workflow modules passed. The workspace at `http://127.0.0.1:4173/` lists five finished sessions: 6 October in `warehouse_btc5m`, 15 October in `warehouse_btc5m_s3`, 19 October in `warehouse`, and 1 October plus 3 October in `warehouse_phase_b_validation`.

Ticking the 6 October session returned 60 windows whose start is inside `14:36:26Z`–`19:36:35Z`. The table starts at `14:40Z`. The `14:30Z` window started before the capture, and markets scheduled after the stop stay outside the block. Exploratory 95% with provider-reported settlement included 15 of those 60 and excluded 45. Complete coverage on the same block included 0. The notebook window `btc-updown-5m-1791302100` (`17:55Z`) is inside the recording and is excluded as `book_below_floor`: Up book about 281s / 300s, Down book about 281s / 300s, reference 1200s / 1200s. Notebook 06 replayed that window without this floor. This pass did not run `strategy_replay.run_window`, so trades were not compared.

Saved set `7470720940814968bac80839d60e1636`, name `6 Oct exploratory 95%`, stores the block id. The first limitation sentence said the result had been checked against later provider metadata. That check had not been run: the detector looked at family attributes, while `priceToBeat` is in the gamma provider metadata (`additional_fields`). The sentence was corrected. Replay was not started. The bound experiment from that pass still accepts only `CONFIRMED_CANONICAL` until it is bound again. The earlier sets `a623f7b6d3cc4525b5310d3613072020` and `fe35312b19df44a8bcfa2eebfb0580a2` are unchanged. No order, paper launch, or new recording was started. This is not Data Library acceptance and does not qualify live trading.

## Replay journey, 22 October 2026

Implemented and covered by `tests/unit/research/test_replay_preview.py` plus the existing workflow job tests. A later correction the same day made `replay.validate` run the scenario validator, made `results.open` return full-configuration totals that ignore the ledger page, and made new traces keep decision clocks and the engine ledger. The same three test modules then passed 23 tests. `ruff check` passed on the touched workflow modules. Existing job directories were not rewritten.

| Operation | What it does | Still gated |
|---|---|---|
| `strategy.capabilities` | Settlement-value fields, essentials, groups, friction/favorable scenarios, and YAML bases. ask70 and z_gap are reported unavailable. | No second strategy adapter |
| `replay.validate` | Refuses a missing or mismatched exploratory policy. Resolves the full settlement configuration. Does not invent policy fields. | Sets whose `research_policy` is not `exploratory` |
| `replay.preview` | Builds units from the saved selection. Whole set, `holdout` null, `objective` null. Same strategy and execution share one experiment. | The browser must not send units |
| `replay.history` | Paged runs and one card per strategy. | — |
| `job.start` for `mode: replay` | Does not call the development/holdout check. A second start of the same experiment returns `job_exists`. | Non-replay experiments still use that check |
| Worker part files | Decision traces are written beside the part. Market ids that contain `:` use a safe file name; the unit id inside the part is unchanged. | — |

Fixture checks are not the warehouse run. The warehouse run is in the Step 4 record. Tune, profile export, paper launch, and a new recording were not started.

## Replay result contract, 23 October 2026

| Change | Where | Why |
|---|---|---|
| Parts store the market's five minutes as `window_start_ns`/`window_end_ns`, the loaded range as `range_*`, and `window_clock_contract: market_interval_v1` | `worker.py` | The earlier part stored the load range (300 s warm-up first), so every ledger label was five minutes early |
| Earlier parts are relabelled from the saved set; the old values become `range_*`; without the set the market time is reported unavailable | `results.py` | Nothing is rewritten on disk |
| `position` (side, shares, VWAP, debit, fees, sales, payout) kept per part; earlier filled parts rebuild it from a saved engine ledger or say `not_stored` | `worker.py`, `results.py` | The ledger can show size, partial fills and exits |
| `execution_state`: `no_entry`, `not_sent`, `no_fill`, `partial`, `filled`, `exited` | `results.py` | A request is not a fill and a fill is not an exit |
| Trace `replay_trace_v3`: market interval, load range and a `leg` on every ledger event | `worker.py` | Fill markers need the leg; the engine ledger only says BUY/SELL |
| `job.start` refuses a second job of an identical experiment digest while the first is preparing, running or stopped (`identical_replay_active`) | `jobs.py` | Two identical previews started two parallel 32-unit jobs on 23 October |
| `replay.history` lists started jobs only, reads the job folder once, reports `total` and `previews_without_job`, and filters `previewed` explicitly | `replay_setup.py` | Previews were counted as runs |
| `strategy.capabilities` gives every field a label and a unit, typed execution fields, deduplicated YAML bases, saved configurations and frozen candidates as bases | `replay_setup.py` | The editor showed raw keys and duplicate files |
| New `configuration.save` (validated, immutable) and `candidate.list`; `candidate.save` keeps configuration identity, execution, policy and `origin: replay` | `replay_setup.py`, `results.py`, `api.py` | Reusable configurations and result-backed candidates |
| Replay preview no longer reports the notebook holdout overlap; `results.py` imports `strategy_lab` only inside the evaluation guard | `replay_setup.py`, `results.py` | Replay has no split |
| `results.open` caches the prepared population per job manifest and returns batch, execution, set size and creation time | `results.py` | Configuration and ledger switches no longer reread every part |
| `selection.list` returns the set's date range and accepts `selection_id` | `recipes.py` | The Replay set card no longer waits for a full pin check |

Tests: `tests/unit/research/test_replay_results_contract.py` (new, 9 cases) and the updated `test_replay_preview.py`; `tests/unit/research` passes 185. Saved jobs, sets and the split file were not modified.

Still gated: Tune, export, paper launch. Candidate save and freeze are wired to the existing operations but were not exercised in the browser; export stays disabled until the research-tolerance guard is verified.

## Delete, trash and cleanup, 23 October 2026

| Operation | What it does |
|---|---|
| `selection.delete` | Moves a saved set to `trash/<entry>/`. Refused while a running replay reads it. Runs that used it stay and show "Set deleted". |
| `replay.delete` | Moves one run (experiment, all its jobs, and its batch once empty) to the trash. Refused while the job is preparing or running. Works for unstarted previews. |
| `trash.list` / `trash.restore` | Lists entries with origin and file groups; restore moves every file back, refused if the same identity exists again. |
| `trash.purge` | The only operation that removes bytes. Requires `confirm: "delete permanently"`. |

`selection.list` reports `status` (`current`, `legacy_policy`, `not_replayable`) and `replay.history` reports `status` (`current`, `legacy_scenario`, `set_deleted`). The Data Library shows the status chip, a Delete control with an in-page confirmation, and a Trash panel; Results history rows have Delete. Tests: `tests/unit/research/test_workflow_trash.py`.

Cleanup moved 14 items to the trash (all restorable): 2 failed jobs, 1 notebook-era experiment, 5 unstarted previews, 1 duplicate run, the 2 first verification runs whose traces predate the engine ledger, 1 duplicate set, and 2 sets that could never be replayed (partial without policy, missing-inputs diagnostic). Kept: 1 current set and 4 legacy-policy sets; 2 current-scenario runs and 5 legacy-scenario runs (including the penalty comparison behind decision 3).

The workspace server no longer allows address reuse on Windows. Four servers had been listening on 4173 at once and requests reached whichever the OS picked; a second start now exits with "Port 4173 is already in use".
