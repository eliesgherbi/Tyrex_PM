# Step 4 — Frontend over complete workflows

> **History.** Superseded by [STATUS.md](STATUS.md) and `Docs/latest/`. Kept as the record of how this was decided.

Status: draft for review. Screens may be sketched during Steps 1 and 2. Implement each offline interface slice once its corresponding Step 3 operation is repeatable without the interface. The user has deferred the remaining [Step 2 follow-up](02_step2_follow_up.md) while independent Step 3/4 work proceeds; paper execution/restart/reconstruction acceptance and full trading completion remain open.

## Goal

Make the qualified settlement workflow operable from a small interface, with status taken from system evidence.

## Objective

A person can move through data, research, and paper rehearsal for the hold workflow, see whether each step is ready, running, failed, or unqualified, and open the underlying evidence. The interface adds no strategy rules and no execution path.

## Requirements

### First scope

The first version covers four areas:

| Area | Shows and does |
|------|----------------|
| Data | Collections, coverage, quality, and preparation status for a chosen identity |
| Research | Experiment configuration, launch and resume, comparison, and exploration of results |
| Runs | Paper status, positions, orders, blockers, and explanations |
| Evidence | What is verified, what failed, and what remains unqualified, tied to revision and scenario |

Live controls are absent from the first version. They are added only after the live workflow they would operate has its own qualification record.

### Behavior

Every action calls a Step 3 operation. The screen sends the same inputs the CLI sends and renders that operation's outputs, progress, and errors.

The evidence view uses the inventory labels: implemented, tested, and qualified stay distinct. A research result can be explored and compared. The screen does not present it as a live or paper qualification.

Choosing a candidate and opening evaluation data remain explicit human actions. The interface can show which data is still unused and which comparison is valid. It does not auto-select a winner.

Paper run views identify the profile, the ledger, and whether the run is flat, exposed, or blocked. A flat run is displayed as a flat run.

### Bounds

The first version is the hold workflow that Step 3 made repeatable, plus the data and evidence needed to operate it. Optional exits, re-entry, and the second strategy plugin appear when their workflows meet the same bar. The ML lab can link to the data area. It does not gain trading controls here.

The interface reads evidence. It does not write a parallel qualification file that the CLI does not know about.

## How to achieve it

1. During Steps 1 and 2, sketches may mark the four areas and the human decisions. Sketches are not the build.
2. Once the corresponding Step 3 operation is implemented and reviewed, bind that screen action to it and define the evidence fields it may display. Offline slices need not wait for all of Step 3; paper-operation acceptance still requires the relevant Step 2 evidence. The Data Library binding order is [step_03_data_library_backlog.md](step_03_data_library_backlog.md): the warehouse view connects after DL-1 only. Recipe, window-set, and experiment-open actions wait for DL-2 and DL-3. Preparation controls wait for DL-4. `prototypes/ihm-review/` is a sketch, not the operation contract. Paper launch, a live recording session, and ask70 capture stay gated and do not block browsing recorded data.
3. Build data, research, and paper operation against the hold workflow. Include the evidence view in that same version so qualification status is visible beside results.
4. Exercise the workflow through the interface: select a known collection, validate preparation, configure the reference experiment, preview, run or resume, inspect, compare, and open the paper rehearsal record.
5. Confirm the displayed status matches the operation outputs and the inventory for that revision.
6. Leave a written gap list for live controls. Do not add the controls in this version.

## Completion criteria

- A person completes the hold workflow's data, research, and paper sequence from the interface without using notebook-only state.
- Each screen action maps to one Step 3 operation, and a failed operation shows that operation's error.
- The evidence view shows verified, failed, and unqualified items for the revision on screen, and those items match the inventory.
- Research P&L and paper qualification are displayed as different kinds of evidence.
- The interface contains no order planner, no strategy decision rule, and no live submit control.
- Resume, identity mismatch, and an unsupported hold-versus-exit combination behave as they do from the CLI.
- This document is reviewed again against the finished Step 3 contracts before implementation is treated as authorized.

## Integrated workspace, 21 October 2026

`frontend/` is the integrated application. `prototypes/ihm-review/` stays the synthetic reference and is not served by the workspace. The integrated bundle aliases React to `frontend/node_modules` so the shared chart component does not load the prototype's second copy.

The local bridge is `python -m tyrex_pm.research.ihm --host 127.0.0.1 --port 4173`. It serves `frontend/dist` and `POST /api/workflow`, which calls `workflows.call` with workflow root `var/research/workflows/ihm`. Warehouse roots come from `TYREX_WAREHOUSE_ROOTS` or, when that is unset, `var/warehouse_btc5m` if that directory exists. `collection.start` and `paper.preflight` with `launch: true` return `launch_gated`.

Connected operations:

| Page | Operations | Availability shown when missing |
|---|---|---|
| Data | `library.windows`, `library.collections`, `library.window`, `data_recipe.resolve`, `selection.save`, `selection.list`, `selection.open`, `preparation.plan`, `preparation.start`, `preparation.status`, `preparation.stop` | Coverage tracks, exclusion reasons, and `safe_stop: unsupported` |
| Research | `strategy.capabilities`, `replay.validate`, `replay.preview`, `replay.history`, `job.start`, `job.status`, `job.stop`, `job.resume`, `results.open`, `results.trace`, `results.compare`, `candidate.save` | Replay does not call `experiment.bind`. Tune, export, and paper launch stay unavailable. Unknown P&L stays "Unknown". A missing trace shows `available: false`. |
| Runs | `paper.preflight` | Launch stays disabled |
| Evidence | `paper.status` | Closed gates are listed: Step 2 follow-up, paper launch, ask70 capture, new recording |

The warehouse heatmap is built from the returned windows. Synthetic explorer series are not loaded on these pages.

Launcher, from the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File E:\polymarket\Tyrex_PM\tools\start_ihm.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File E:\polymarket\Tyrex_PM\tools\stop_ihm.ps1
```

`start_ihm.ps1` installs `frontend` dependencies only when `frontend/node_modules` is absent, builds `frontend/dist`, then starts the bridge. On 21 October those two stages were run separately while confirming `http://127.0.0.1:4173/`: `npm run build` in `frontend`, then `python -m tyrex_pm.research.ihm --host 127.0.0.1 --port 4173`. The script itself was not started as one process because that port was already serving this application.

## Replay screen, 22 October 2026

The Research page follows the approved mockup at `prototypes/ihm-review` (`http://127.0.0.1:4174/research.html`). Data Library is unchanged except the handoff label **Use in experiment**. The integrated application is `http://127.0.0.1:4173/`.

Checked in the browser, light and dark:

- Data Library still lists the five recordings and seven saved sets. **Use in experiment** on `6 Oct exploratory 95%` (`7470720940814968bac80839d60e1636`) opened Replay with 15 windows, `2026-10-06 14:55 UTC – 2026-10-06 18:25 UTC`, and policy `exploratory95_v1`.
- Choosing the 32-window set `68a995b5269b45e593c0d44d7acb8e78` replaced that draft. The previous configuration was not kept.
- Two configurations, Hold 3c edge and Hold 4c edge, validated on the server. Preview reported 15 windows, 1 experiment, 30 units, holdout none, and 1 notebook-holdout window kept in the set.
- The first start, job `7ee7c2cc3fd24f43bea1ab1df8a085e9`, failed and the page showed the refusal: Windows rejected `:` in `parts\<market-id>.json.tmp` (`WinError 123`). That job stays in history as failed. No result rows were invented for it.
- After part files were given safe names, a new preview started job `d977aa70930643ca9e98e3588e2cd002` on experiment `9e5f3ecca314488bb6b6c0c4852f7dad`. Stop left it `interrupted` at 4/30 with the same job id. Resume continued as generation 2. At the last status read it was `running`, 8/30. This pass did not wait for all 30 units.
- The open dashboard for that job showed 15 windows on the set, qualified and unqualified rows apart, and unknown P&L rather than zero. One stored window was an entry request with no fill, unqualified for `order_constraints_unqualified`. Explorer drew the saved Up/Down series (630 decision rows) and left fields that were absent on the selected row as unavailable. Orders and attempts stayed separate from fills.
- Refreshing the application kept both jobs in history and opened a clean Replay draft. Filtering history to running left the open dashboard on `d977aa70930643ca9e98e3588e2cd002`.

Not exercised, and not claimed: a completed 30-unit batch, `candidate.save`, `results.compare` of two finished jobs, Tune, profile export, paper launch, and a new recording. The design mockup on port 4174 was left running.

Later the same day, the results page and Explorer were corrected against the review. Dashboard totals now come from the full configuration, so a ledger filter cannot change them. Execution validation uses the scenario validator before preview. New traces keep `at_ns`, window bounds and the engine ledger, and a unit is not marked done until that trace is published. Older traces stay readable and say when the decision clock was not stored. No saved job was overwritten, and Tune stays deferred.

## Replay screen rebuilt on the approved components, 23 October 2026

The 22 October screen was a rewrite of the mockup and drifted from it ([review](reviews/replay_integration_review_2026-10-22.md)). The Research page now ports the approved components themselves into `frontend/src/research/`:

| File | Ported from | Data source |
|---|---|---|
| `ResearchWorkspace.tsx` | `ResearchWorkbench.tsx` (without its shell) | `selection.list`, `replay.preview`, `job.*`, `replay.history`, `results.*`, `candidate.*` |
| `ReplayBuilder.tsx` | `ReplayBuilder.tsx` | `strategy.capabilities`, `replay.validate`, `configuration.save` |
| `ReplayResults.tsx` | `ReplayResults.tsx` | full-population summary from `results.open`; history paged by `replay.history` |
| `ReplayExplorer.tsx` | `ReplayExplorer.tsx` | one saved trace from `results.trace` |
| `chartOptions.ts` | `options.ts` | same palette, axes and legend; UTC clock from the trace |
| `SavedSetChoices.tsx`, `TunePreview.tsx` | same names | real saved sets; Tune stays a design-only Phase 3 preview |

`App.tsx` now carries the mockup shell: workspace chip, nav icons, the RESEARCH sub-navigation, the round theme toggle and the `research-v2 workflow-v3` scope on `.app-shell`. The Research workspace stays mounted while browsing other pages, and its draft, open run, configuration and window survive a refresh in session storage. The earlier `ReplayWorkspace.tsx`, `ReplayExplorer.tsx` and `replayCharts.ts` were removed.

Checked in a real browser on 4173, light and dark, with no console errors:

- Data → Window sets → **Use in experiment** on the 82-window set opened Replay on `1d4f9704…` with 82 windows and 5 recordings. Choosing the 32-window set from **Choose / change window set** started a clean draft.
- One configuration validated on the server; a second was refused with `order_delay_ms = -1` and then accepted at 300 ms. Preview: 30 units, 1 experiment, 15 windows, every effective parameter and all 27 execution assumptions expandable.
- Started `dca680bcb90e4364ae0ea5ad433558ce` (Edge 3c, 15 windows) from the page, stopped it at 0/15, refreshed (the stopped job was still shown), resumed it as generation 2 and let it finish: 15/15, 6 qualified, 9 requests, 1 fill.
- Results opened the latest run automatically. A ledger filter left the four metrics unchanged. History search kept the open dashboard. Switching configuration on `d977aa70…` changed the owner strip, metrics, chart and ledger together.
- Explorer on the filled window of `dca680bc…`: request triangle on the model axis, fill at 0.170 on the price axis, warm-up from the stored load range, Entry zoom, inspector reading Enter · Edge clears cost with own ask 0.170, and the request → arrival → fill → payout table. The same window on an earlier job explains that its trace has no ledger and draws no fill marker.
- **New replay** cleared the draft and kept six started runs in history.

Screenshots: `reviews/replay-integration-2026-10-23/`.

Not exercised: candidate freeze and export, Compare on two finished configurations of one set (the view exists), Tune, paper launch and a new recording. Preview still takes about 90 s on this machine because it verifies every pinned file.
