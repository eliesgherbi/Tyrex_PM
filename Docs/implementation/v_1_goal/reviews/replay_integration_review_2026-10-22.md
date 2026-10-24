# Replay integration review — 22 October 2026

Verdict: the integrated application follows the broad journey, but it is not yet a faithful implementation of the approved mockup. The largest gaps are the Explorer, explanation of empty results, result-bundle completeness and a few correctness issues. This is a review, not implementation acceptance.

Reviewed the integrated application on port 4173, the approved mockup on port 4174, frontend and backend source, and saved job artifacts. No replay, collector, paper session or real order was started. No application code, configuration or saved result was changed. Only this report and review screenshots were added. Read-only execution validation calls were used to reproduce a defect.

## What the saved runs actually contain

Latest completed job: `7725ebffc58c4f639eb81b1b7f7d2f9d`, configuration `My first replay`, selection `7470720940814968bac80839d60e1636`.

- 15 completed window/configuration units.
- 13 qualified windows, two unqualified windows.
- Two entry attempts, no fills. Both saved attempts ended as `ZERO_FILL`, reason `unfilled_cancelled`; the ledger does not expose this reason.
- Every stored `realized_pnl` is null.
- Both entry windows carry `order_constraints_unqualified`.

Earlier completed job: `d977aa70930643ca9e98e3588e2cd002`.

- 30 units: two configurations on the same 15 windows.
- Across those units: 17 units with an entry attempt and two filled units.
- Both filled units are unqualified, with `gap_with_exposure` and `order_constraints_unqualified`; both have diagnostic P&L values.
- No qualified unit has a stored numeric P&L.

The empty qualified P&L chart is therefore consistent with these saved records. It is not proof that execution failed to run or that all charts lost their data. The interface fails to make this understandable. The engine also leaves P&L null for no-fill windows; displaying that as unknown currently follows its contract. Do not convert every null to zero. Define an explicit no-trade state, and distinguish it from missing economics or unresolved position valuation.

`order_constraints_unqualified` means the simulator lacked the instrument's tick or minimum-share constraint at order submission. It is a qualification limitation, not itself an order-rejection reason. The actual no-fill / not-sent reason must come from the saved attempt. Source: [engine.py](E:/polymarket/Tyrex_PM/src/tyrex_pm/research/replay/engine.py:114).

## Functional issues to fix first

### F1 — Ledger filtering changes the run's summary

**Priority: P1. Reproduced in the browser.**

Opening the latest job shows 13/15 qualified. Choosing **Unqualified** in the window ledger changes the dashboard to 0/2 and changes the chart population. The table filter is applied before the summary and chart are computed.

Keep three separate populations: all rows for the selected configuration, the authoritative full-population summary, and the filtered/paged ledger. A ledger filter must not alter the run's performance or qualification denominator.

Source: [ReplayWorkspace.tsx](E:/polymarket/Tyrex_PM/frontend/src/ReplayWorkspace.tsx:288).

### F2 — Execution validation accepts invalid settings

**Priority: P1. Reproduced through read-only validation calls.**

`replay.validate` returned `valid: true` for each of:

- `order_delay_ms: -1`.
- `timer_interval_ms: 0`.
- `fee_policy.rate: "2"`.

`_execution` checks field names, locked metadata and participation, but does not run the complete scenario validator. These settings can fail only when the worker loads the generated scenario.

Validate the complete typed scenario before marking a configuration valid or creating its preview. Return field-specific errors. Preserve the effective execution snapshot returned by validation.

Sources: [replay_setup.py](E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/replay_setup.py:320), [config.py](E:/polymarket/Tyrex_PM/src/tyrex_pm/research/replay/config.py:66).

### F3 — The saved decision trace loses the replay clock

**Priority: P1. Confirmed in source and saved traces.**

The original replay contains `at_ns`; `_compact_trace` drops it. It retains `observed_at` where available, but that is absent on host-skipped records. One inspected trace has 631 rows, including 301 host-skipped rows, but only 330 rows have `observed_at`.

The frontend plots row numbers 0–630 as equally spaced categories. Those positions are neither seconds nor trustworthy elapsed time. This distorts the duration of gaps and events and prevents faithful alignment with orders.

Preserve exact decision time, window bounds and available input timestamps in a versioned trace contract. Use a UTC time axis and preserve unavailable intervals. Do not invent times for historical records that lost them; show their limitation or regenerate their traces explicitly.

Sources: [worker.py](E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/worker.py:152), [ReplayWorkspace.tsx](E:/polymarket/Tyrex_PM/frontend/src/ReplayWorkspace.tsx:391).

### F4 — The persisted bundle is too small for the approved Explorer

**Priority: P1. Confirmed by source inspection.**

The runner produces full economics, engine ledger, position information, holds, protection events, seals and detailed decision fields. The workflow keeps a narrow summary plus selected decision fields, orders and attempts. The detailed engine fill ledger is omitted. Orders and attempts retain useful fill information, but cannot replace every event in that ledger.

Publish an evidence bundle sufficient for position size, fill price/time, fee, remaining shares, decision-to-order-to-fill alignment, K and policy/provenance inspection. Existing saved artifacts should remain readable with honest availability flags. A chart redesign alone cannot restore evidence already discarded.

Sources: [run.py](E:/polymarket/Tyrex_PM/src/tyrex_pm/research/strategy_replay/run.py:1139), [worker.py](E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/worker.py:120).

### F5 — A unit can be committed before its trace is safely published

**Priority: P1. Failure path identified in source; no crash injected during this review.**

`_commit` writes the summary and its manifest entry before writing the trace. The manifest hashes only the summary. Resume skips units already recognised as done. A failure between those writes can leave a completed summary with a missing trace that resume does not repair.

Publish and verify all required bundle components before marking the unit complete. Test interruption at that boundary and trace corruption/missing-file recovery.

Source: [worker.py](E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/worker.py:35).

### F6 — Large results can silently become first-page results

**Priority: P1 for scaling. Confirmed in source; current 15/30-unit jobs fit the page.**

The frontend requests `results.open` with limit 500, ignores `next_cursor`, and computes dashboard totals from the returned rows. With several configurations, page boundaries can also give different configurations different incomplete populations. The server reads all summary files before slicing the page.

Provide authoritative per-configuration aggregates across the complete run and server-side ledger filtering/pagination. Retrieve traces on demand. Add a regression case exceeding 500 units.

Sources: [ReplayWorkspace.tsx](E:/polymarket/Tyrex_PM/frontend/src/ReplayWorkspace.tsx:263), [results.py](E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/results.py:18).

### F7 — Partial/failed results do not have a distinct dashboard state

**Priority: P1. Confirmed by rendering and response contracts.**

History lets the user open failed or running jobs, but `results.open` does not return job state and expected/completed progress. The frontend renders the normal result dashboard whenever a result object exists. It does not clearly withhold final totals for partial results or explain an empty failed job there.

Expose completion state and distinguish partial, failed and completed views. Preserve inspectable partial rows without presenting them as a final population.

Sources: [results.py](E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/results.py:29), [ReplayWorkspace.tsx](E:/polymarket/Tyrex_PM/frontend/src/ReplayWorkspace.tsx:359).

### F8 — Batch control needs stronger failure handling

**Priority: P1/P2. Source inspection; not exercised by launching new jobs.**

When starting multiple experiments, earlier jobs can start before a later start fails. The function returns before storing those earlier job handles in React state. Stop/resume responses are ignored. Network errors lack consistent handling, and asynchronous open-result/trace responses have no stale-response guard.

Retain every started job immediately, report partial batch start, handle control refusals, and prevent a late response from replacing a newer selected run/window. A completed or failed batch needs a direct fresh-replay action.

Source: [ReplayWorkspace.tsx](E:/polymarket/Tyrex_PM/frontend/src/ReplayWorkspace.tsx:246).

## Explorer: differences from the approved mockup

The integrated chart is a replacement with three basic lines, rather than the approved chart connected to real data. Its title promises a comparison that the Down panel does not currently provide.

1. **Down model is missing.** The Down panel draws only the Down ask. Under the binary model, display `P_down = 1 - P_up` when that identity is valid, with provenance. Do not imply the Down ask is its model estimate.
2. **Conservative estimates are missing on both legs.** The trace already stores Up lower/upper estimates. The corresponding Down conservative estimate is `1 - p_up_high` under that same model convention.
3. **Book midpoints are missing.** Up/Down bid and ask are already saved for many rows. Compute a midpoint only where both required quotes are valid; retain gaps.
4. **No leg labels, series legend or units.** Restore UP/DOWN labels, P [0,1], USDC/share, UTC and the approved line meanings.
5. **Wrong clock.** Replace decision index with actual event time, as described in F3.
6. **No linked crosshair or visible selected-time marker.** The approved upper/lower panels and inspector follow one instant together.
7. **No Full window / Entry / After entry controls, zoom slider or warm-up context control.** Show warm-up only when actual context is available; do not generate it.
8. **No request, arrival, fill or sale markers.** Restore evidence-based markers and their legend, preserving the distinction between intent and execution.
9. **No lifecycle navigation or event table.** The approved Decision → order → fill table is absent even though orders/attempts contain useful events.
10. **No previous/next window navigation or window selector.** Users must return to the ledger for every change.
11. **No window summary.** Market time range, acquired position, provider-reported winner, P&L and qualification context disappear on entering Explorer.
12. **Inspector is reduced to a few raw fields.** Restore Decision / Execution / Inputs / Identity tabs, formatted values, timestamp, time remaining, scrubber, conservative P, own-leg ask and execution details.
13. **Point P has ambiguous leg ownership.** The implementation always shows `p_up_point`, even if the chosen leg is Down. Label it P(Up), or show the chosen-leg value correctly. This is a semantic issue, not decoration.
14. **Poor initial moment.** The selected index defaults to the first row, which in the inspected trace is an unavailable host-skipped context. Explain it or choose a useful explicit initial instant; it should not make the whole trace appear absent.
15. **Dark appearance is not applied to Explorer.** It receives no theme/palette and uses ECharts defaults: bright grid lines, faint/default axes and unrelated line colours. Restore the approved muted grid and consistent blue/teal/neutral palette in both themes.
16. **Chart click is narrower than the approved behaviour.** Only clicks on series points use `onCategory`; clicking anywhere on the timeline should select a time and update the linked inspector.

Sources: [integrated Explorer](E:/polymarket/Tyrex_PM/frontend/src/ReplayWorkspace.tsx:380), [approved Explorer](E:/polymarket/Tyrex_PM/prototypes/ihm-review/src/ReplayExplorer.tsx:8), [approved chart options](E:/polymarket/Tyrex_PM/prototypes/ihm-review/src/options.ts:16).

Screenshots show different real/synthetic data and viewport sizes; they compare visual grammar and controls, not numerical performance:

- [Empty-results dashboard](E:/polymarket/Tyrex_PM/Docs/implementation/v_1_goal/reviews/replay-integration-2026-10-22/integrated-empty-results.png).
- [Integrated Explorer](E:/polymarket/Tyrex_PM/Docs/implementation/v_1_goal/reviews/replay-integration-2026-10-22/integrated-explorer.png).
- [Approved Explorer](E:/polymarket/Tyrex_PM/Docs/implementation/v_1_goal/reviews/replay-integration-2026-10-22/approved-explorer.png).

## Results and history: missing details

- **Explain the empty performance view.** Show a run funnel: 15 windows → two attempts → zero fills, alongside 13 qualified/two unqualified. Distinguish all attempts from qualified-only rates. Link the two exceptional windows and their attempt reasons. The current 0% qualified entry rate is scoped, but alone hides the fact that two attempts occurred.
- **Keep unqualified windows discoverable on the outcomes chart.** The mockup uses explicit baseline markers, excluded from P&L. The implementation removes them entirely and has no chart-to-Explorer click action.
- **Do not render an unexplained empty axis.** For this run, explicitly say no qualified numeric P&L was published and why. Preserve null values. A diagnostic view may show unqualified economics separately, clearly excluded from qualified totals.
- **Restore the dashboard's companion explanation panel.** The approved qualification/count breakdown and no-holdout context are missing.
- **Restore names and ownership.** The result heading is an experiment hash; the input set is another hash. Use batch/set/configuration names, a clear owner strip and scenario/policy, with full identities available in secondary details.
- **Use readable window times.** The ledger and chart categories show long condition hashes, not chronological UTC windows. Rows are sorted by unit id; join saved window metadata and order chronologically. Keep full ids in details/copy controls.
- **Show richer execution status.** No entry, not sent, no fill, partial fill, filled and exited should remain distinguishable. Translate raw reason codes into concise explanations with the raw code available.
- **History is always expanded.** Restore strategy → compact optional history → selected dashboard. The default currently requires a separate click rather than opening the latest actual run automatically.
- **Run counts include previews.** The inspected strategy says five runs, but the five history rows are three actual jobs and two previews with no job. Distinguish drafts/previews from started runs. A newer preview can also make a strategy card's latest job null while older completed jobs exist.
- **History search excludes the job id.** It searches experiment id, names and selection id. Include job id and friendly saved-set name. Expose total count/page count, configuration count and UTC labels consistently.
- **Partial loading states look like true emptiness.** Initial history briefly reports zero saved runs and Data briefly reports no recordings before requests complete. Show loading or error states rather than asserting absence.

Sources: [result rendering](E:/polymarket/Tyrex_PM/frontend/src/ReplayWorkspace.tsx:351), [history operation](E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/replay_setup.py:599).

## Configuration, preview and navigation differences

- The main essentials/groups and explicit Data handoff are present and should be retained.
- Validated configuration rows lost **Edit** and **Save configuration**. There is no reusable named-configuration collection. The base menu is a list of YAML names, including duplicate visible basenames, without source/grouping clarity.
- Parameter annotations show `text`/`number` instead of useful units such as seconds, USDC or USDC/share. Raw enum names and long decimal strings need presentation formatting without changing stored precision.
- Execution fields use generic text boxes instead of typed controls. Changes-from-base counts, base source detail and the approved price-penalty summary are missing.
- Validation fingerprints hash only strategy parameters, while preview fingerprints include execution too. The displayed identity can change between validation and preview, and different execution assumptions can initially appear identical. Use one explicitly scoped identity convention.
- **Preview does not expose effective parameters or execution assumptions.** It shows configuration names and experiment ids. Restore expandable authoritative resolved snapshots before Start, not merely counts.
- The selected Data control navigates away to Data instead of offering the approved inline saved-set chooser. More seriously, leaving Research unmounts the entire workspace, losing the draft, selected result and job-control state even when the user is only browsing another page. Preserve context separately from an intentional new-set/new-replay action.
- New replay clears the configuration list but not all editor values/names/scenario state. Make fresh draft versus explicit reuse unambiguous. Provide it on the Replay progress/error screen too.
- Batch naming and friendly configuration/result labels need consistency: everything defaulting to Replay/My first replay makes history difficult to scan.
- Tune's unavailable state is appropriate for the agreed scope. Compare/candidate/export differences are later work; they should not distract from finishing Replay and Explorer.

Sources: [editor and preview](E:/polymarket/Tyrex_PM/frontend/src/ReplayWorkspace.tsx:330), [workspace mounting](E:/polymarket/Tyrex_PM/frontend/src/App.tsx:92), [validation identity](E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/replay_setup.py:374).

## Recommended correction order and acceptance

1. **Protect correctness:** F1/F2, completion-state handling, full-population summaries and one stable configuration identity. Regression checks must show that ledger filtering cannot change totals and that invalid execution settings are refused before preview.
2. **Publish a sufficient evidence contract:** exact clocks, window context, economics and engine events; atomic verified trace publication. Keep historical limitations visible. Do not silently overwrite existing results.
3. **Restore Explorer from the approved components:** preserve its structure, styling and controls; adapt its data inputs to the real bundle. Do not reuse fixture calculations or interpolate through invalid books.
4. **Finish result explanation and workflow parity:** no-fill/qualification explanations, readable UTC windows, chart markers, full preview inspection, configuration edit/reuse, compact history and persistent draft/navigation state.
5. **Review both themes on real recorded runs:** a no-entry window, an attempted/no-fill window, and an existing unqualified filled window. Confirm that event markers agree with saved execution times and that every displayed number belongs to the selected run/configuration/window.

Add meaningful checks for more than 500 units, delayed/out-of-order UI responses, interrupted trace publication, failed/partial job views and complete scenario validation. The existing replay-preview tests do not demonstrate visual parity or these edge cases.

Do not change trading thresholds, weaken qualification, convert unknown economics indiscriminately to zero, or fabricate successful fills to make the dashboard resemble the mockup. Preserve the mockup's visual design while honestly representing real data.

## Status, 23 October 2026

Addressed by the rebuild recorded in `04_frontend.md` and `step_03_implementation_record.md` (23 October): F1, F2, F3 and F5 were already fixed on 22 October; F4 (positions, leg-tagged ledger), F6 (server totals, paged ledger and history, cached population), F7 (partial and failed views), F8 (every started job kept, refusals shown, stale responses guarded) and Explorer items 1–16 are now implemented on the approved components. Window labels were found five minutes early and are corrected. A >500-unit regression case is still missing.
