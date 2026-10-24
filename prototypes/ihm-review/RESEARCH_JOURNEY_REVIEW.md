# Research journey design review · 21 October 2026

**22 October update:** the current review at `research.html` uses the revised `ResearchWorkbench.tsx`. See [the V3 alignment note](RESEARCH_WORKFLOW_V3_REVIEW.md). The configuration presets, development-population wording and aggregate economics below describe the earlier proposal and are superseded by the new Replay/Tune contract. Earlier source and screenshots are retained as design history.

Open **http://127.0.0.1:4174/research.html**. This is a separate review app built from the earlier Tyrex prototype, using its brand, theme, Data Library sketch and paired ECharts panels. The integrated workspace on port 4173 is unchanged.

From this directory, run `npm run research:dev`. Build validation: `npm run research:build`.

## What changes from the earlier design

1. **Experiment first.** Keep a saved input set as the stable context. Select a named, versioned strategy configuration and execution scenario; inspect effective values. Define the question, baseline, metrics and acceptance criteria before previewing the workload. Changing setup invalidates its review. A learned probability source remains unavailable until a compatible artifact exists.
2. **Results as a population.** Account for all windows and distinguish requests, full/partial fills, no fill, no eligible entry, blocked inputs and unresolved outcomes. Separate known exploratory economics from qualification. An input-blocked window has unknown research performance, not a fabricated zero. Show metric denominators.
3. **Explorer as an explanation.** Results → window → selected moment. Preserve the Up and Down charts, point and conservative model probabilities, each leg's own book, request/arrival/fill markers and visible input gaps. Align the inspector and execution ledger to the selected timestamp. Inspect decision, execution, inputs and identity separately. The ask70 sketch omits model probabilities and shows the book threshold instead.
4. **Comparisons with a declared question.** Match exact windows and source/engine assumptions. Show parameter differences, overlap, exclusions and unknown pairs. Refuse a configuration-only improvement when execution assumptions differ; an explicit sensitivity mode can describe a combined change. Open the changed baseline decision.
5. **Candidate as a snapshot.** Preserve the explored configuration, result, pinned set, execution assumptions, baseline, criteria, limitations and rationale. Reopening restores that configuration and result identity. Candidate save is a research checkpoint.

## Suggested review tour

- Experiment: inspect effective parameters; preview; change the edge configuration and verify that a new preview is required. Play the demo replay.
- Results: inspect the losing window, partial fill, no fill, unresolved outcome and blocked window. Filter the window ledger.
- Explorer: select Entry, Fill and Input gap; inspect Inputs and Execution. Click a chart timestamp or an execution event. Check both legs and their price/probability units.
- Compare: switch B to delayed arrival to see the compatibility refusal; align assumptions; inspect the changed window; preview saving B.
- Candidates: save a browser-only candidate and reopen it. Its configuration and result identity should match the snapshot, even after a different experiment was edited.

## What this prototype actually does

All eight windows, prices, model outputs, metrics, identities and execution events are **illustrative fixtures**. A replay button animates local state. Candidate saves use session storage. The Data page is the earlier mock Data Library, and its handoff is explicitly acknowledged without pretending that those selected windows were replayed. No warehouse/API calls, Python jobs or orders are made.

One-window chart data is constructed only for the current window. This demonstrates the intended presentation, not a production storage or performance implementation. A prototype comparison reads two fixture variants; it is not an execution-engine comparison.

## Backend contracts needed for integration

- Saved selection identity, exact pinned membership and research admission/limitations; an explicit backend-built experiment preview with effective parameters and a stable preview identity.
- Named configuration catalog and versioned strategy chart adapter capabilities; unavailable features must explain their requirement. Ask70 has no alternative comparison configuration in this sketch.
- Atomic, versioned result bundle publication containing identity and summary, with decisions, model/quote/reference observations and execution events as separate trace resources. Preserve original reason codes and assessment flags. A missing trace stays unavailable.
- Full-population summary and paired comparison independent of the visible ledger page. Unknown and unqualified results must remain distinguishable. Summary first, paginated rows second, one selected window's bounded trace last; do not load all traces on page open.
- Immutable candidate snapshot with full effective parameters, strategy/model/source versions, selection pins, result identities, metrics, assumptions, limitations and rationale. Reference real evidence rather than rewriting/recomputing it on open.
- Actual job progress/stop/resume and publication identity. The browser animation is not evidence that those operations work.

Review this screen flow before wiring it. Keep notebook/CLI callers on the same backend operations; the browser must not build replay units or reproduce trading logic.

## Verification performed

- TypeScript checking and the production review build passed.
- Browser walkthrough: setup preview → animated replay → results → losing-window inspection; click-to-time chart synchronization; fill and invalid-input inspection; result filtering including the input-blocked window.
- Comparison: a different arrival-delay scenario withholds the configuration comparison; aligning assumptions restores the paired view.
- Candidate: save B, inspect the saved configuration/result identities, and reopen that snapshot. Candidate save remains browser-only.
- Strategy adapter: ask70 omits probability outputs and shows its book threshold. Light and dark layouts were inspected. Browser error logs were empty during the final walkthrough.
- Screenshots are saved under `review-artifacts/research-journey/`. These prove prototype behavior only.

The review build currently includes ECharts in a roughly 1 MB uncompressed JavaScript bundle and emits a chunk-size warning. Before production integration, split route/chart loading and measure actual summary, paginated-row and selected-trace request sizes and latency. The fixture app is not a warehouse performance benchmark.
