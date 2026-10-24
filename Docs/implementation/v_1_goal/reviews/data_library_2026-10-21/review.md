# Data Library: workflow, performance, and design review

Reviewed 21 October 2026. Scope: the integrated application at http://127.0.0.1:4173/, the Data Library service contracts, and the handoff into research. Application and warehouse inspection was read-only. No preparation, save, experiment binding, replay, recording, or order was started. Only review artifacts and isolated test fixtures were written.

## Verdict

The recording-block model is a sound foundation. The application is now a useful functional prototype with actual warehouse browsing, pagination, time/outcome filters, reviewed selections, and persisted file identities. It is not yet ready for acceptance as a dependable quant research workflow. Research partition enforcement, preservation of result limitations, manual picking, reproduction of a saved set, and the amount of work required to browse or pin data need attention.

Keep the current React/Python/ECharts stack. The largest improvements come from correcting contracts and indexing existing information, rather than replacing the framework.

## Evidence and limits

- Used the running application: chose the 6 October recording, paged its table, previewed an Up-only population, exercised manual picking, opened a window inspector, navigated Data → Research → Data, and inspected both themes. No draft was saved.
- UI warm sample: Next displayed windows 26–50 of 60 in approximately 76 ms including the browser action/read. Up-only preview completed in approximately 330 ms including the browser action/read. These are individual observations, not percentiles.
- Fresh Python process, same local archive: first `library.windows` read took 75.3523 s; a subsequent page took 0.3183 s; hourly coverage 0.3196 s; Up-only recipe preview 0.3498 s; listing six selections 0.4975 s. The cold sample includes service work, not process startup. This was a shared-load local review, including concurrent tests; it is not an isolated performance benchmark or a measured UI network latency.
- The 25-row first page serialized to approximately 174 KB, the next page 118 KB, the preview 40 KB, and the six-set list 4 KB. These are uncompressed JSON sizes measured at the service boundary.
- The existing 15-window selection is about 5.57 MB and pins 6,237 files across 1,743 collection manifests. Its existing experiment file is 58,518,220 bytes for 15 units.
- Existing saved selections intersect the authoritative frozen evaluation split. The named 15-window exploratory set contains 1 reserved market; each of the two existing 82-window sets contains 4. This does not establish that a new evaluation was run. It establishes that the saved development/exploratory population and reserved partition are not isolated.
- Verification: 23 tests passed in 12.37 s for `tests/unit/research/test_data_library.py` and `tests/unit/research/test_workflows.py`; frontend TypeScript check passed with no emit. The first test attempt hit the sandbox's temporary-directory rename restriction; using a workspace-local fixture directory resolved the environment issue.
- No new real-data replay was run. Million-window behavior, concurrent browser load, and cold/warm percentiles remain unmeasured.

## Findings, ordered by impact

### R1 — P1: enforce the evaluation partition on the server

**Observed:** several saved research sets contain markets from `frozen_split().holdout`. Inventory derives its reserved set solely from caller-supplied `reserved_market_ids`; the browser sends none. Consequently these markets are assigned `development`, their outcomes can be exposed, and recipe filtering does not withhold them.

**Business impact:** selecting by outcome can contaminate the reserved evaluation population. A claim of untouched out-of-sample performance becomes unreliable. Relabeling an already exposed market does not undo the exposure.

**Recommendation:** resolve the authoritative split server-side. Apply it before reading or counting outcomes and enforce it again at save, bind, and job start. Explicit diagnostic/evaluation access should use the existing exposure-recording journey. Audit existing saved sets and flag affected ones; do not silently rewrite their membership or claim the holdout is untouched.

**Acceptance:** a normal UI request without a client-supplied exclusion list withholds all reserved labels/counts and cannot save or bind those markets as development. Explicit diagnostic access leaves an exposure record.

Sources: [inventory.py:771](/E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/inventory.py:771), [recipes.py:167](/E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/recipes.py:167), [staged.py:265](/E:/polymarket/Tyrex_PM/src/tyrex_pm/research/strategy_lab/staged.py:265).

### R2 — P1: carry execution qualification into saved results

**Observed:** the worker returns P&L, entry/fill booleans, evaluation count, and a decision digest, but drops `engine.qualified`, `engine.limitations`, label provenance, fee provenance, and the detailed replay evidence. The results page can therefore show a numerical P&L with only the generic experiment limitation. An individual `gap_with_exposure` or unqualified order-constraint result is lost at this boundary.

**Business impact:** a researcher cannot distinguish which windows have additional execution defects or unsupported economics. A successful job is easily confused with a reliable result.

**Recommendation:** persist an immutable full result, or an immutable result reference plus a faithful summary. Carry per-window qualification, reasons, labels, policy versions, and missing-value handling into results and comparison denominators. Keep unqualified exploratory rows visible with explicit labels. Do not equate job completion with qualification.

**Acceptance:** a replay fixture that reports `gap_with_exposure` remains visibly unqualified through the worker, saved part, results endpoint, and UI; its P&L cannot enter an unlabelled qualified aggregate.

Sources: [worker.py:90](/E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/worker.py:90), [results.py:31](/E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/results.py:31), [Operations.tsx](/E:/polymarket/Tyrex_PM/frontend/src/Operations.tsx).

### R3 — P1: make browsing and pinning proportional to the requested work

**Observed:** pagination limits the response only after `_scan` has loaded the collection metadata, resolutions, manifests, and quality JSON. Recording/date filters are applied afterward. Cache keys include combinations of roots and collection filters, so a new combination can rescan previously visited roots. Even warm requests enumerate build/quality directories for their revision stamp. Paging also repeats the unchanged hourly-coverage query.

Saving fingerprints every file from the whole collection. Reopening and binding verify the same broad set again. Every experiment unit embeds the complete collection manifest list; 15 units produce a 58.5 MB experiment. Every replay unit constructs a new Reader and WindowCatalog. Listing selections reads every complete large JSON before returning a small page.

**Business impact:** first reads can take over a minute with only a few recordings; saving or inspecting a small set can take minutes. Large experiments scale in metadata volume as windows × manifests. A spinner improves feedback but does not reduce the work.

**Recommendation:**

1. Publish an indexed window summary during existing normalization/quality publication. Include collection/block, timestamps, coverage numerators and denominators, outcome provenance, qualification, and assessment revision. Inventory reads stay read-only.
2. Query that summary with source/date/partition predicates before pagination. Compute range aggregates separately and cache them by the filter/revision, independent of the table page.
3. Keep intervals, raw metadata, and detailed evidence out of list rows; load them for the inspected window only.
4. Use one immutable snapshot manifest referenced by selection and replay units. Preserve seeds, warm-up, metadata, and later resolution dependencies when narrowing pins; do not simply cut to the trading interval.
5. Retain checksum verification, but perform expensive verification as explicit background work with durable progress/result identity. Reuse evidence for genuinely unchanged immutable files under a documented invalidation policy; check execution inputs before replay.
6. Reuse Reader/catalog/prepared-window state within a block worker. Read a lightweight saved-set index instead of opening every large selection JSON on list.

**Acceptance:** representative 1/10/100-recording fixtures demonstrate bounded first-page work; changing pages does not rescan the archive or recompute an unchanged chart; experiment metadata grows approximately with units plus shared snapshot size. Record cold/warm timings, response bytes, files read, and memory. Suggested product targets, to validate rather than claim: ordinary cached filtering under 500 ms and a first summary under 2 s, with larger preparation explicitly asynchronous.

Sources: [inventory.py:528](/E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/inventory.py:528), [inventory.py:542](/E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/inventory.py:542), [recipes.py:393](/E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/recipes.py:393), [recipes.py:590](/E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/recipes.py:590), [recipes.py:703](/E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/recipes.py:703), [experiments.py:249](/E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/experiments.py:249), [worker.py:90](/E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/worker.py:90).

### R4 — P2: manual picking removes its own candidate list

**Reproduced:** choose Custom window set → Scope → Up → Pick individual windows → Resolve. Pick one window. The candidate list disappears immediately. Refreshing shows that the pick was retained, but selecting several windows now requires a refresh after each click.

**Cause:** candidates and the final validated preview share the same state. Every picked-id change clears that preview, and the candidate list renders only while it exists.

**Recommendation:** keep the matching-candidate page and draft picks separate from the validated final preview. Changing picks should stale the final count, not erase the controls. Add selected count, select matching/all on page, clear selection, paged candidates, and a clear final revalidation step. Server validation remains authoritative.

**Acceptance:** pick three windows consecutively, unpick one, change page, and return without losing either the list or selection. Removed-by-filter picks remain explained and cannot bypass requirements.

Sources: [DataLibrary.tsx:596](/E:/polymarket/Tyrex_PM/frontend/src/DataLibrary.tsx:596), [DataLibrary.tsx:966](/E:/polymarket/Tyrex_PM/frontend/src/DataLibrary.tsx:966).

### R5 — P2: preserve a reproducible selection recipe and navigation context

**Observed:** Data → Research → Data resets the selected recordings and local filters because DataLibrary is unmounted. `Use filters for a new set` restores only filters, recipe, and name. It does not restore recording ids, coverage floor, settlement confidence, collection, or dates. `selection.open` does not return the complete creation scope/requirements contract.

**Business impact:** an apparent repeat of the same selection can use different data or requirements. Returning from an experiment leaves the researcher rebuilding context.

**Recommendation:** expose and persist a complete selection specification, separate from frozen membership: block ids, date/time filters, source choices, requirements/policy versions, and intended use. Reuse should clone that specification into an editable draft and re-resolve it, showing changes against the saved membership. Preserve draft and workspace context in the route or a shared state store; reopening a saved set must continue to use its pinned inputs.

**Acceptance:** save/reopen/reuse a 99% outcome-filtered multi-block set after navigating elsewhere. The draft retains exactly the same specification and any membership difference is explicit.

Sources: [App.tsx:68](/E:/polymarket/Tyrex_PM/frontend/src/App.tsx:68), [DataLibrary.tsx:496](/E:/polymarket/Tyrex_PM/frontend/src/DataLibrary.tsx:496), [data.py:selection_open](/E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/data.py:327).

### R6 — P2: distinguish captured data, research eligibility, and qualification

**Observed:** the warehouse shows 0 Inputs ready and 60 Coverage gap, while the selected recording can yield 15 exploratory windows. The current chart uses a strict availability classification and does not say which research policy it represents. Coverage and settlement confidence are separate backend fields, but the UI bundles them into Complete / Exploratory 95% / Exploratory 99% presets; the semantic requirement also branches on coverage mode rather than independently enforcing the confidence choice. Outcome labels still say Reference-derived proxy even though they come from provider payout observations. Warm-up in inventory is currently the reference-feed coverage repeated; it does not establish all strategy indicator warm-up requirements. Saved cards omit the numerical floor unless it was typed into the name; the old partial set still offers Open experiment despite having no explicit exploratory policy.

**Recommendation:** show an explicit active use/policy, then a funnel: recorded windows → matches → usable for this research policy → excluded. Keep physical coverage separate from semantic verification. Show Up book, Down book, reference, indicator warm-up, provider winner, and settlement confidence as distinct concepts. Read strategy-required inputs from a server-owned requirement profile rather than letting raw checkboxes imply readiness. Keep outcome-conditioned sets clearly diagnostic and never present them as representative evaluation samples.

**Acceptance:** 0 strict-qualified windows and 15 exploratory windows can coexist without contradiction. Winner provenance is labelled Provider-reported outcome (or payout-derived proxy). Changing the policy updates the same summary used in preview. Coverage and settlement-confidence combinations are explicitly supported or refused, with each choice enforced. Indicator warm-up corresponds to the configured strategy and input series. Old sets have a clear compatibility status.

Sources: [inventory.py:493](/E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/inventory.py:493), [DataLibrary.tsx:187](/E:/polymarket/Tyrex_PM/frontend/src/DataLibrary.tsx:187), [DataLibrary.tsx:982](/E:/polymarket/Tyrex_PM/frontend/src/DataLibrary.tsx:982).

### R7 — P2: preparation needs an explicit target and a truthful operation

**Observed:** Prepare data picks a collection from a window on the current table page and immediately calls plan then start for normalization only. It does not prepare all ticked recordings or only selected dates. There is no review of the plan. Current saved recordings are already normalized. Safe stop is unsupported while the owner call is running.

**Recommendation:** replace the ambiguous action with a preparation plan showing target recordings/collections, outstanding normalization, quality assessment, and verification, plus work that preparation cannot repair. Disable no-op actions. Start only the chosen work and show its durable status. Do not offer a stop as if interruption is implemented when it is not.

**Acceptance:** changing the table page cannot change the preparation target; selecting two blocks shows both targets; an already normalized collection is not described as requiring normalization; no processing starts from the review action.

Source: [DataLibrary.tsx:507](/E:/polymarket/Tyrex_PM/frontend/src/DataLibrary.tsx:507).

### R8 — P2: finish source-bound queries, request recovery, and pagination

**Observed:** a one-window inspector sends the general scope with all configured roots rather than the clicked row's warehouse/collection/block. This can start a broad scan and can select the first alternative source for a duplicated market. There is no request identity guard on the inspector result. Emptying all selected blocks does not invalidate an already in-flight warehouse generation. Several loading/preview paths lack `try/finally`, so a network exception can leave a perpetual busy state. Saved-set listing ignores `next_cursor`; the coverage view fixes a 500-bucket cap and ignores further pages. Duplicate-source requirements are displayed as a message without controls to resolve the choice.

**Recommendation:** bind every detail query and response to the clicked source identity; cancel or ignore stale responses, including transitions to an empty selection. Handle transport failures with visible retryable states and durable save identities. Page all growing lists and use adaptive chart buckets plus an explicit range/limit. Implement the source-choice contract before allowing ambiguous sets to appear saveable.

**Acceptance:** slow response A cannot replace the detail for B; a selected duplicate opens the selected source; deselecting everything stays empty after old requests finish; disconnection exits busy state; more than 100 sets / 500 chart buckets / multiple sources remain reachable.

Sources: [DataLibrary.tsx:333](/E:/polymarket/Tyrex_PM/frontend/src/DataLibrary.tsx:333), [DataLibrary.tsx:776](/E:/polymarket/Tyrex_PM/frontend/src/DataLibrary.tsx:776), [api.ts](/E:/polymarket/Tyrex_PM/frontend/src/api.ts), [inventory.py:970](/E:/polymarket/Tyrex_PM/src/tyrex_pm/research/workflows/inventory.py:970).

### R9 — P2: improve information hierarchy and stable color meaning

**Observed visually at approximately 1280 × 720:** the restrained dark/light themes and logo work, and the new coverage meters are useful. However, important table and help text is 9–11 px; long condition identifiers spill into adjacent columns; recording cards are ordered by warehouse rather than date and use 0.09 h for a five-minute session. Their progress-like bars represent relative duration and turn green when selected, which resembles a quality/processing indicator. U/D/R/W still have no visible legend. The chart axes lack explicit Window count / Window start (UTC) titles. Exclusion group colors depend on array order, so a reason can change color after a filter. Modal drawers have no visible keyboard-focus management in the implementation. The compact sidebar hides its text without replacement icons at smaller widths.

**Recommendation:** keep the palette and general shell. Use accent only for selection/actions, green for usable, amber for partial/conditional, red for absent/failure, and neutral for unknown/withheld with explicit text. Never use red/green alone to distinguish Up/Down from quality. Use 12–14 px ordinary text, stable reason colors, ellipsized ids with copy/details, human durations, chronological sortable recordings, visible legends, labelled axes, and focus-managed drawers. Move internal codes, filesystem names, and qualification detail into a secondary inspection area. Replace Resolve on the server with Preview matching windows, and Composition with Review inputs.

**Acceptance:** the key workflow is understandable at desktop and a narrow sidebar without internal identifiers or unexplained letters. Keyboard navigation and both themes retain labels, selection state, and legible text. Formal contrast ratios were not measured in this review.

Sources: [workspace.css](/E:/polymarket/Tyrex_PM/frontend/src/workspace.css), [style.css](/E:/polymarket/Tyrex_PM/prototypes/ihm-review/src/style.css), [DataLibrary.tsx](/E:/polymarket/Tyrex_PM/frontend/src/DataLibrary.tsx).

## Recommended user journey

1. **Choose recordings:** searchable, chronological recordings with asset, duration, normalization state, assessed coverage, and policy-specific usable counts. Dates narrow actual recording blocks; empty time between recordings is not invented.
2. **Define the population:** date bounds, local daily hours/time zone/weekdays, outcome and source, plus matching/all versus manual picks. Clearly mark retrospective outcome-conditioned diagnostics. Show candidates continuously and maintain a selected count.
3. **Review inputs:** explicit research policy and coverage floor; matches/kept/excluded; per-input reasons, source choices, and preparation plans. Unknown and unavailable remain distinct.
4. **Save and open research:** name and immutable membership plus complete reproducible recipe, source revision, and assumptions. Save has durable progress and a request identity; returning later reopens the same inputs. The experiment retains the selected strategy/scenario identity and per-window limitations.

## Delivery order

1. R1 and R2: protect research integrity and preserve truth in results.
2. R4, R5, R7, and the request/source correctness in R8: make the actual journey reliable.
3. R3: indexed reads, reusable immutable snapshots, background verification, and lighter replay units. Measure again with a representative larger archive.
4. R6 and R9 alongside the above: policy-aware summaries, business wording, legibility, stable colors, and responsive/keyboard checks.

Do not broaden to other page enhancements or live-launch qualification until the Data Library journey has a concrete acceptance walkthrough. The corrections already present—recording blocks, table pagination, async preview waiting, preview invalidation, shared in-flight inventory reads, provisional K acceptance on exploratory bind, and extraction from provider metadata—should be retained.

## Visual evidence

![Warehouse dark theme, strict availability chart and compact input labels](/E:/polymarket/Tyrex_PM/Docs/implementation/v_1_goal/reviews/data_library_2026-10-21/warehouse-dark.png)

![Warehouse light theme, recording cards and filter hierarchy](/E:/polymarket/Tyrex_PM/Docs/implementation/v_1_goal/reviews/data_library_2026-10-21/warehouse-light.png)
