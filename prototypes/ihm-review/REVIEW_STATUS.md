# IHM design review status

Date: 21 October 2026 (Europe/Paris).

This is a standalone React + ECharts prototype, not an integrated trading/research frontend. Source is confined to prototypes/ihm-review. Dependencies and generated output are ignored locally. No Python strategy, notebook, run configuration, saved research evidence, account or ledger was changed.

## Verified

- TypeScript check and production build passed. Vite reports a large chart bundle; code splitting is a later integration concern, not a claimed production performance result.
- validate-fixtures.mjs passed: scope/counts, resolved-fill denominators, unknown P&L, request versus fill coordinates, independent leg books, conservative Down mapping, gap behavior, diagnostic marker suppression, no-fill closure and ask70's missing model / uncertain exit.
- The browser rendered actual ECharts charts. A missing graphic-component registration was corrected during review.
- Browser interactions checked: window selection updates the chart and inspector; a click on the chart changed the inspected instant from +58 to +176 seconds; partial-fill selection; warm-up; diagnostic view hides original fill rows; ask70 omits probability controls; paired comparison; the job UI animation starts/stops/resumes/completes; data gap filtering and preparation preview; candidate handoff; disabled paper launch; Evidence navigation; light and dark appearance.
- screenshots/research-dashboard.jpg captures the full Research dashboard with illustrative fixtures.

## Review boundaries

All model series, prices, outcomes, fees, execution events and progress are synthetic fixture data. Sample scalar metrics and EV are fixture arithmetic, not new replay results. Job animation never changes the saved result bundle. Candidate choices and notes use sessionStorage in the current browser tab only.

Static P4/P5/P6 blocker labels follow the supplied agent feedback and retained implementation record; they are not live backend checks. No new qualification, collection, replay, paper session or real order was performed.

The next decision is approval or adjustment of layout, information hierarchy, chart semantics and workflow navigation. Backend integration and its acceptance tests follow that review. EXPLORER_MAPPING.md lists the current source meanings and proposed per-strategy view boundary.

## Data Library redraw — 21 October

The former fixed synthetic table/pin button is replaced by Warehouse, Window sets and Recording sessions. DATA_LIBRARY_REVIEW.md describes the screen contract; DATA_LIBRARY_BACKEND_PROMPT.md contains the separate specification-only backend handoff.

Browser checks on the redraw: UTC date filtering narrowed the 24-window fixture to eight; availability filtering; an incomplete-reference inspector; protected outcome inspection; manual selection of two windows through Goal/Scope/Composition/Save; a named demo-set-001 with fixed membership and preview ID; handoff to an experiment showing two windows and four illustrative comparison units; preparation animation completion and stop; explicit alternative recording selection updated preview membership and identity. Saved sets and demo processing are sessionStorage only. Normalization, coverage, reference choices, labels and all seven overview tracks are fixtures, not warehouse measurements.

The new experiment draft never opens the existing eight-window Explorer as results of its selected set. Real execution remains disabled. No Python source, Step 3 specification, warehouse, configuration or evidence was changed by this redraw. TypeScript/build and the existing Research fixture checks passed; the production chart bundle warning remains.

## Custom window-set filters — 21 October

Manual-only case selection is extended with filter-first custom sets and optional individual picks. Scope offers outcome, label source, daily hours, named clock zone and weekdays, with outcome/provenance in the candidate table. Presets and custom sets share these rules. Fixture v3 adds 18 daily-clock examples while preserving all original v2 identities/timestamps. Saved sets retain filters and support a new draft from those filters. Reversal/volatility/liquidity controls explain unsupported measurement definitions.

Verification: `validate-data-filters.mjs` passed for half-open daytime/overnight ranges, UTC/Paris and DST/weekday boundaries, outcome/provenance AND filters, invalid inputs, protected getters never read, preserved original identities and copied filter snapshots. Existing Research fixture checks passed. Browser checks: Up + 08:00–20:00 matched six development candidates in the current recovery collection; provider confirmation narrowed that to one. Individual/bulk picks and weekday exclusion were checked. A saved `demo-set-002` retained its Up/time/provenance filters, opened a diagnostic experiment draft, and remained unchanged when reused in a Down-filter draft. Future reversal availability reasons were inspected.

This remains browser-only data. `CUSTOM_FILTERS_BACKEND_NOTE.md` is the required DL-2 contract follow-up; no production filter query or data scenario was run.

Backend contract review corrected the UI's proxy wording to “Payout-derived proxy”, matching the existing `proxy_outcome` classification of recorded payout vectors. Provider-confirmed mock examples remain fictional; the supported real-data filter can legitimately return no rows. No production label classification was changed.
