# Research IHM enhancement

> **History.** Superseded by [STATUS.md](STATUS.md) and `Docs/latest/`. Kept as the record of how this was decided.

Date: 22 October 2026. Status: design agreed, not implemented. This document is the single reference for the research workbench from this date. The earlier Step 3 and Step 4 documents, the Data Library backlog, the 21 October review, and the implementation record remain as history and are not the plan any more. Nothing here authorizes a collector start, a paper session, or an order.

## 1. Why this document exists

The Data Library page works. The Research page is a thin proof that buttons reach the backend. Between the two, the work drifted toward making the page agree with the notebooks, and the page inherited rules from notebook-era code that nobody had asked for on the screen. The result was confusing: three different "ready" rules, a development/holdout split applied silently at bind time, saved sets that refuse to bind, and an acceptance test that compared the page with a notebook that calls the same engine.

This document states the objective, describes the current state factually, names what went wrong, fixes the vocabulary, describes the target design, and gives the roadmap from here to there.

## 2. Objective

The project has four layers, and the IHM is the way to use them.

1. **Record.** Capture live market data and store it normalized, checksummed, and reusable, so later work can trust it.
2. **Trade.** A set of modules that lets us build a strategy and run it, in paper or real mode, through the one existing trading host.
3. **Research.** A place to validate and explore strategies before they trade: replay recorded data through the strategy engine (backtesting), look at decisions and results, tune parameters, and keep what works.
4. **Operate from one screen.** Research, in practice, is many runs with many parameter changes and a lot of back and forth. That workflow belongs in a user interface, not in notebook cells. The IHM is that interface. It started with data because every research step starts from data. It continues with research, then candidates, then paper.

The research workbench is finished when a person can, from the screen and without notebooks: pick recordings, save a window set, run a strategy on it with chosen parameters, read and compare the results, tune parameters with an honest out-of-sample check, save a candidate, and export a runnable configuration.

## 3. Where we are

### What exists and works

- **Warehouse.** Recording, normalization with per-file checksums, and quality publication per 5-minute market. Five finished recordings across four warehouse folders. The 6 October recording covers 14:36 to 19:36 UTC and contains 60 windows that start inside the capture.
- **Replay engine.** `src/tyrex_pm/research/replay/engine.py` runs a strategy over a recorded stream of books and reference prices and scores simulated fills. `src/tyrex_pm/research/strategy_replay/run.py` loads one window from the warehouse into that engine. Both are proven on fixtures and used by the notebooks.
- **Workflow operations.** `src/tyrex_pm/research/workflows/` exposes one JSON envelope per operation for the CLI, notebooks, and the browser: window listing and coverage, recipe preview, selection save and open, experiment bind, job start, status, stop, results open, candidate save and freeze, profile generation and validation, paper preflight.
- **Data Library page.** `frontend/src/DataLibrary.tsx` at `http://127.0.0.1:4173/`. Tick recordings, read coverage, preview a window set under a rule, save it. Preparation shows a plan. Stop is unsupported and says so.
- **Research page.** `frontend/src/Operations.tsx`. Bind a saved set, start a job, poll it, open results with per-window qualification and limitations. Candidates tab exists.
- **Candidate to paper.** `workflows/profiles.py` turns a frozen candidate into a paper YAML with explicit operational fields and validates it with the real run-config loader. Launch stays gated.

### What exists but is not usable yet

- The Research page sends `parameters: {}`. There is no way to set strategy parameters from the screen, although `experiment.bind` accepts a single parameter set, an explicit list of configurations, or a grid.
- The Candidates tab sends `strategy_kind` and empty parameters with no `result_ids`. It writes a candidate file that is linked to nothing.
- No replay job has been run from the screen on real warehouse data. The worker is proven on fixtures only.
- Results show per-window rows and two counts. There is no run summary, no decision timeline, no comparison between two runs, and no resume from the page.

### What is wrong today

- **Three readiness rules.** The coverage chart on the Data page marks a window ready only when every input is complete. The set preview uses "exploratory 95 %": each book covers at least 95 % of the window, provider-reported winner, provisional K accepted, $1/$0 payout. The notebooks used no floor. Same data, three answers, three names.
- **A hidden split.** `workflows/partition.py` imports `frozen_split` from `strategy_lab/staged.py`. That split was written once by a notebook-era command into `var/research/strategy_runs/s2/split/split.json`, cuts each recording chronologically 70/30, and is never recomputed. The Data Library did not know about it until 21 October, and now applies it at save, bind, and job start. Sets saved before that date contain holdout windows and refuse to bind. The user never chose this rule and cannot see it on the screen.
- **A hidden parameter overlay.** `strategy_lab/experiments.py` defines `RESEARCH_BASE_PARAMETERS`, which accepts provisional K, and merges it under every notebook experiment. `experiment_bind` does the same for exploratory sets. The configuration that actually ran is not the one the user typed.
- **The wrong referee.** The acceptance test for the data journey became "the page and the notebook agree on one window." The notebook calls the same engine the page calls. The comparison would prove that two wrappers pass the same bytes, not that the engine is right, and it makes the notebook the standard the screen must match. The notebook should become a reader of what the screen saves, not the other way round.
- **One intent for every run.** "Replay" in the workflow means one thing in the engine and several things in use: a single configuration on some windows, a grid over parameters, a qualification run. Because the notebooks needed the grid-and-holdout path, its rules leaked into every run. A user who wants to see what one configuration does on one afternoon gets a tuning workflow.
- **Documents.** Seven overlapping documents describe the same work from different angles. None of them is the plan.
- **Nothing is pinned.** About 60 modified tracked files and the whole of `frontend/`, `src/tyrex_pm/research/workflows/`, `Docs/implementation/v_1_goal/`, `tools/`, and the new tests are uncommitted. No result recorded today can name the code that produced it.

## 4. Principles

1. **The IHM is the research product. Notebooks are clients of it.** `workflows/` does not import from `strategy_lab/`. When the screen does something, a workflow operation owns that behaviour, and a notebook later opens the same saved object by id.
2. **The user states intent; the tool records and enforces exactly that.** No rule is inherited from notebook history or applied silently. Policy, parameters, mode, split percentage, objective: each is chosen on the screen and stored with the object it belongs to.
3. **A set is data.** It says nothing about how it will be used.
4. **An experiment states its mode.** Replay or Tune. Only Tune introduces a development/holdout split, as one of its own parameters, with the holdout stage built into it.
5. **A finished job is not a qualified result, and a research result is not a paper qualification.** Qualification and limitations travel with every row.
6. **Correctness is proven by deterministic tests and recorded baselines, not by agreement with a notebook.**
7. **Nothing is rewritten.** Old sets, old results, and the old split file are kept as history and marked superseded where needed.

## 5. Vocabulary

These are the only nouns the screen, the code, and the documents use. One word, one meaning.

- **Recording.** One capture session from start to clean stop, in one warehouse folder. The 6 October recording is one recording.
- **Window.** One 5-minute market. A window belongs to a recording when its start falls inside that capture.
- **Coverage.** How much of each required input was physically recorded for a window, as a number per input: Up book, Down book, reference price, reference warm-up, provider outcome.
- **Policy.** A named, versioned readiness profile: minimum book coverage, reference warm-up length, accepted outcome source, accepted K quality, payout assumption. Examples: `strict_v1` (every input complete, confirmed K only), `exploratory95_v1` (books at least 95 %, provider-reported outcome, provisional K accepted, $1/$0 payout). One policy drives the chart, the preview, and the save. The policy is stored in the set and in every configuration that runs on it.
- **Window set.** A frozen list of windows plus the recordings it came from, the policy, the filters, and the file pins. No partition. Reusable by any experiment.
- **Mode.** The intent of an experiment: **Replay** or **Tune**.
- **Configuration.** One fully resolved set of strategy parameters. Nothing is merged in afterwards.
- **Base configuration.** Where a configuration starts: strategy defaults from the schema, a YAML under `config/runs/`, or a saved candidate.
- **Experiment.** A window set, a mode, a strategy, one or more configurations (or a grid), an execution scenario, and, in Tune mode, an objective, a holdout percentage, and a top-k. Immutable once bound.
- **Run.** One execution of an experiment, as a background job with durable status, stop, and resume.
- **Result.** What a run produced: per-window rows with decision, fill, P&L, qualification, and limitations; per-configuration summaries; in Tune mode, the ranking, generalisation, and stability views.
- **Candidate.** One configuration chosen from a result, with the result ids that justify it and a written rationale. Frozen once. Carries its development and holdout numbers when it comes from a Tune, and "no holdout evaluation" when it comes from a Replay.
- **Export.** A candidate turned into a runnable paper YAML, with the preserved and added fields listed. Launch is a separate, gated action.

## 6. Target design

### 6.1 Data Library

What stays: recording cards in time order, ticking, the window table, the coverage chart, preview, save, saved-set list, preparation plan.

What changes:

- **One policy chooser** above the chart. The chart, the funnel, the preview, and the save all use the chosen policy. The funnel reads: recorded windows → match the filters → usable under this policy → excluded, with reasons. The "every input complete" chart and the "exploratory 95 %" preview stop being two different things.
- **Sets have no partition.** The set card shows recordings, policy, filters, window count, and date range. Nothing about development or holdout.
- **The frozen split file is retired as a gate.** The Data Library does not read `split.json`. The file stays where it is as history of the notebook research.
- **Old sets are marked superseded** and hidden by default. Files are kept. They are not rewritten.
- **Outcome wording follows the policy.** "Provider-reported outcome" when the stored source is a provider payout, "proxy" only when it is derived.
- Performance work (an indexed window summary built at normalization time, cached range aggregates) is scheduled when it starts to hurt, not before. First cold read today is about 75 s on one recording.

### 6.2 Research: two modes

An experiment begins by choosing its mode. The mode fixes which windows are used, how configurations are produced, what the result computes, and what the result may claim. Underneath, both modes expand to the same `strategy_replay` units and the same engine.

#### Replay — "What does this strategy do on these windows?"

- Inputs: a window set; a base configuration; one or several explicit configurations edited from it; an execution scenario (`friction` or `favorable`, from `config/research/scenarios/`).
- Windows: the whole set.
- Output: per-configuration summaries side by side (qualified count, unqualified count, entry rate, fill rate, P&L on qualified rows only); per-window table; per-window decision timeline.
- Claim: exploratory. A Replay result can become a candidate; that candidate carries "no holdout evaluation" visibly.
- No objective, no split, no ranking.

#### Tune — "Which parameters are best for this objective, and do they generalise?"

Inputs, all chosen before anything runs:

- a window set
- a base configuration
- a grid: for each parameter to vary, a list of values; every other parameter keeps its base value
- an execution scenario
- an objective: one metric, maximise or minimise, computed on qualified rows only
- a holdout percentage, applied as the chronological tail of each recording in the set (method stored as `chronological_tail`, so other methods can be added later, for example holding out whole recordings)
- top-k: how many of the best development configurations are carried into the holdout stage (default 3)

Bind: the server expands the grid into configurations, cuts the set into development and holdout lists per recording, stores both lists in the experiment, builds development units, and shows the plan: configurations, development windows, holdout windows, total units. Nothing runs until the user confirms. A Tune with 0 % holdout is refused with the reason.

Run, as one resumable job in two stages:

- **Stage 1, development.** Every configuration on every development window. Produces the ranking by objective.
- **Stage 2, holdout.** Only the top-k configurations from stage 1, on the holdout windows. Starts when stage 1 completes. The rest of the grid never touches holdout.

Result, three views:

- **Ranking.** The development table ordered by objective; each row shows qualified and unqualified counts and a per-recording breakdown; each row expands to its per-window rows and timelines.
- **Generalisation.** For each of the top-k: objective on development, objective on holdout, the gap, and whether the development order survives on holdout.
- **Stability.** For each of the top-k: how the objective changes at the neighbouring grid points, and the dispersion of per-window results across recordings.

The tool proposes nothing. The user picks one configuration from the top-k and saves it as a candidate, with both numbers attached.

Why top-k and not the whole grid on holdout: scoring every configuration on holdout and then picking the best holdout score is tuning on holdout, and the generalisation number means nothing. Selection stays on development; holdout only checks. A whole-grid holdout diagnostic can exist later, labelled as such, and a candidate cannot be chosen from it.

Later extensions that fit without changing the inputs or the views: random or Bayesian search in place of grid expansion; walk-forward in place of one chronological cut.

#### Compare

Not a mode, a view. Any two results side by side, across modes. This is also how a candidate's parameters replayed on a new recording are checked against the original Tune.

### 6.3 Parameters

- A base configuration chooser: schema defaults, a YAML under `config/runs/`, or a saved candidate.
- A form generated from the strategy schema (`strategies/settlement_value/schema.py`). In Replay, each field takes one value per configuration. In Tune, each field takes one value or a list.
- Every unit stores its fully resolved configuration. The accepted-K rule moves from `RESEARCH_BASE_PARAMETERS` and from the bind-time merge into the policy, where it is visible, and into the stored configuration, where it is explicit. The hidden overlay goes.
- Research-only tolerances are never inherited into an export. `profile_generate` already refuses them.

### 6.4 Candidate and export

- Save from a result row: strategy, resolved configuration, result ids, rationale. In Tune, the development and holdout numbers are attached. Freeze once.
- Export: one action producing a paper YAML through `profile_generate`, with the preserved and added fields shown, validated by the run-config loader. Launch stays gated.

### 6.5 Evidence

Per candidate: origin (Replay or Tune, experiment id), development result, holdout result or "no holdout evaluation," export status, paper status or "gated." Verified, failed, and unqualified are different things and are displayed as such.

### 6.6 Notebooks

A notebook opens a saved set, experiment, or result by id through the workflow operations and draws with the plotting helpers. `strategy_lab/` shrinks to plots and teaching. Its split, qualification, and experiment-runner code is deleted or redirected to `workflows/`. Notebooks 04 to 06 are kept as history and not maintained.

## 7. Roadmap

Each phase ends with something a person can do on the screen without notebooks, and each Done is stated in the screen's own terms.

### Phase 0 — Reset

- Commit the tree so every later result names a hash.
- This document becomes the plan. Mark `01_` to `04_`, the backlog, the review, and the implementation record as history in the folder README.
- Mark the saved sets `7470720940814968bac80839d60e1636`, `a623f7b6d3cc4525b5310d3613072020`, `fe35312b19df44a8bcfa2eebfb0580a2`, and the two 82-window sets as superseded. Keep the files.
- Remove "the page agrees with the notebook" from every acceptance statement.

Done: committed; README points here; superseded sets hidden by default.

### Phase 1 — Data Library: one policy, plain sets

- Policies as server-owned named profiles. One chooser drives chart, funnel, preview, save.
- Sets store recordings, policy, filters, included windows, pins. No partition field.
- `workflows/partition.py` and the bind-time holdout check are removed. `workflows/` no longer imports `strategy_lab`.
- Outcome wording from the policy.
- Keep: picks that persist, draft that survives navigation, chronological recordings, human durations, request identity on the inspector, `try/finally` on loading paths.

Done: tick two recordings, choose `exploratory95_v1`, the funnel shows per-recording counts, save; the card shows recordings, policy, and count; the Research page binds it without a partition error.

### Phase 2 — Replay mode, parameters, candidate, export

This phase delivers the full basic journey: set → replay → readable results → candidate → exported configuration.

- Mode chooser with Replay enabled and Tune shown as "Phase 3".
- Base configuration chooser and schema-generated form; one or several explicit configurations.
- Hidden overlays removed; every unit stores its resolved configuration.
- Results: per-configuration summaries side by side; per-window table; per-window decision timeline; resume from the page.
- Compare any two results.
- Candidate from a result row with rationale; freeze; export with the preserved/added report; launch gated.
- First real run on the 6 October set recorded as the regression baseline, with the commit hash, set id, experiment id, job id, and per-window digest.

Done: a Replay with two configurations runs on the 6 October set; the two summaries compare on screen; one configuration is saved as a candidate, frozen, and exported to a YAML that validates; a stopped run resumes.

### Phase 3 — Tune mode

- Tune inputs: grid, objective, scenario, holdout percentage, top-k. Plan shown before run.
- Two-stage job, resumable at either stage.
- Ranking, generalisation, and stability views.
- Candidate from a Tune result carrying both numbers. Evidence page shows Tune-origin and Replay-origin candidates with their different facts.
- Base configuration chooser gains "from candidate," so a candidate can be replayed on a new recording and compared.

Done: a 3×3 grid with 30 % holdout and top-3 runs on the 6 October set; ranking, generalisation gap, and stability neighbourhood are on screen; a candidate is saved from it; a Tune requested with 0 % holdout is refused with the reason.

### Phase 4 — Notebooks become clients

- A notebook opens a set, experiment, or result by id through `workflows.call` and plots it.
- `strategy_lab` split, qualification, and runner code removed or redirected.
- Notebooks 04 to 06 archived.

Done: one notebook reproduces a Phase 2 result from its id with no code of its own beyond plotting.

### Phase 5 — Paper, unchanged

Gated on the Step 2 follow-up: gross/net share reconciliation, the two decision mismatches, host-originated protection orders in reconstruction. Nothing in Phases 0 to 4 touches it. Export produces the YAML; launch stays off until that evidence exists.

### Performance, slotted where it hurts

Indexed window summary at normalization time, cached range aggregates, lighter pins. Build when Phase 2 or 3 testing becomes slow, and measure cold and warm on 1, 10, and 100 recordings when you do.

## 8. Out of scope and unchanged gates

- No collector start, paper session, live recording, ask70 capture, or order from the IHM.
- No rewrite of historical sets, results, or the split file.
- No new storage engine.
- Live controls are added only after the live workflow they would operate has its own qualification record.

## 9. Open decisions

- Which metrics are offered as Tune objectives in Phase 3 beyond realised P&L per resolved fill and fill rate. A drawdown-penalised variant is likely but not decided.
- The default top-k (3 proposed) and whether it is editable per experiment (proposed yes).
- Whether `strict_v1` is offered in Phase 1 or only `exploratory95_v1`, given that no live recording currently has a strict-complete window.
