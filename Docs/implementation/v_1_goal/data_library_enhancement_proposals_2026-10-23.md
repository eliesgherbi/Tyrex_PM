# Data Library and set creation: enhancement proposals

> **Status (25 Oct 2026).** Implemented: see the "Implemented" sections at the end and [STATUS.md](STATUS.md).

Date: 23 October 2026. Status: proposals for review. Nothing here is implemented.

Scope: the path from recordings to a saved window set, and from a set to a replay. Each proposal has an id, the evidence behind it, the expected gain, and a rough size (S: under a day, M: one to three days, L: a week or more). Measurements are from the four warehouses (5 recordings, 214 windows inside a capture) on this machine.

## Where the time and confusion go today

| Moment | What the user waits for or sees | Measured |
|---|---|---|
| First Data page after a server start | Window table and coverage chart | `library.windows` over 4 recordings: 185 s cold, then cached in memory until the next restart |
| Opening a saved set (Inspect, Use in experiment, Replay preview) | Every pinned file re-read to rebuild the window catalog | ≈ 90 s per open or preview, repeated each time |
| Replay | One window at a time, one process per job | ≈ 2 min and ≈ 1.4 GB per window and configuration; 32 windows ≈ 70 min |
| Coverage chart | "Strict physical coverage": ready means every input complete | 0 of 1,196 windows can ever be ready, so the chart always shows only gaps |
| Review inputs step | Five raw check boxes (books, reference, warmup, scoring outcome, provider confirmed) | Ignored by Research v2, which is now the default |

## A. Backend: speed and smoothness

| Id | Proposal | Evidence | Gain | Size |
|---|---|---|---|---|
| A1 | **Persistent window index per recording.** Write one compact table per collection at quality publication (window bounds, book and reference intervals, outcome, price-to-beat, Research v2 profile), keyed by the build manifest fingerprint. Inventory reads it instead of rebuilding from parquet and thousands of `report.json` files. | 185 s cold read; in-memory cache lost on every restart | Data page in seconds after a restart | M |
| A2 | **Verify pins once, not on every open.** Record a verification receipt (file path, size, mtime, sha256, verified at) when a set is saved; on open and preview, re-hash only files whose size or mtime changed, and check membership against the A1 index instead of re-reading parquet. | `verify_saved_catalog` rebuilds the full `WindowCatalog` on each open and preview (≈ 90 s) | Preview in seconds; integrity guarantee unchanged | S–M |
| A3 | **Load a window once, replay it for every configuration.** Group units by window inside a job so a 2-configuration batch loads each window once; cache the decoded event stream per window on disk keyed by pins. | Loading dominates unit time; two configurations load the same window twice | ≈ 2× faster for 2 configurations, more for grids (Tune) | M |
| A4 | **Parallel workers per job** with a bounded pool (for example 3), respecting memory. | 1 window at a time; 1.4 GB per worker; the machine ran 3 jobs in parallel without trouble | ≈ 3× wall-clock | M |
| A5 | **Long operations as background tasks with progress.** Inventory, set preview and pin verification report "read 3 of 5 recordings", like replay jobs already do. | The browser waits on a single request for up to 3 min with a generic message | No more frozen-looking screens; cancellable | M |
| A6 | **Fix the book reconstruction blips** at the source (update/snapshot ordering, or switch coverage and replay to `venue_top_v1`). | 9,923 crossed-book blips of 3–17 ms; they made "complete" impossible and caused every `gap_with_exposure` | Cleaner coverage; the 250 ms tolerance becomes a safety net instead of a necessity | M |
| A7 | **Re-normalize warehouses** so stored metadata carries the minimum-size unit directly. | Engine currently interprets older rows and logs it | Housekeeping; no result changes | S |
| A8 | **Recorder resilience (operational, outside the IHM).** Find why the CLOB socket starts dropping every window after 1–3 hours and fix reconnect. | In each recording drops become continuous after 1–3 h (6 Oct from 15:45, 15 Oct from 17:30, 19 Oct from 13:05); 140 of 214 windows affected | The largest single gain in usable data | M–L |

## B. Front end: visualisation and interaction

| Id | Proposal | Evidence | Gain | Size |
|---|---|---|---|---|
| B1 | **Replace "Strict physical coverage" with a policy coverage chart.** Bars per hour: kept under the chosen policy, excluded by reason (book holes, reference, outcome, not started), using the same rule as preview and save. | The current chart shows 0 ready everywhere and contradicts the preview | One rule on screen, as the design document requires | S |
| B2 | **Window map inside the Data Library.** The barcode from the decision page (one row per window, holes drawn across the 5 minutes, kept rows full, excluded rows faded, entry band shaded) for the ticked recordings and for every saved set. | It was the most effective view in the decision page; users currently see only counts | See why a window is in or out at a glance | M |
| B3 | **Recording cards that say what is inside.** Window count, windows kept under Research v2, share of book time lost, and "connection degraded after HH:MM". | Cards show only time range and folder name | Pick good recordings before building a set | S |
| B4 | **Simplify the set builder for Research v2.** Hide or disable the five raw check boxes when a named policy is chosen; show the policy's fixed rules as a readable summary; one Preview button (it appears on two steps today); compact date inputs. | Check boxes are ignored by Research v2; duplicate buttons on Scope and Review | Fewer wrong expectations, shorter path | S |
| B5 | **Saved set card, fuller.** Date range, recordings, policy status (done), how many runs use it (with links), last used, and a mini window map. Rename and description. | Cards show count, policy chip and limitation text only | Find and trust the right set | S–M |
| B6 | **Window detail view for data only.** From any window in the map: Up/Down books and reference over the 5 minutes with holes marked, K, outcome and provider price-to-beat, before any replay. Reuses the Explorer chart. | Today a window can only be inspected after a replay | Diagnose data before spending replay time | M |
| B7 | **Trash with sizes and age.** Show disk size per entry and suggest purging entries older than N days. | Trash now exists without sizes | Safe, informed cleanup | S |
| B8 | **One theme for the Data Library and Research.** The Data Library uses the older style sheet; Research uses the approved mockup's style. Align spacing, tags and buttons. | Visible difference between the two pages | Consistent product | S–M |

## C. Workflow: what a researcher will need next

| Id | Proposal | Why it matters | Size |
|---|---|---|---|
| C1 | **"Re-save under current rules".** One action on a legacy set: rebuild the same recordings and filters under Research v2 as a new set, linked to the old one, and show what changed (windows added and removed). | 4 legacy sets remain; today this is manual | S |
| C2 | **Set lineage and dependents.** Set → runs → candidates, visible from each side; deleting a set lists exactly what depends on it. | The trash already counts referencing runs; make it navigable | M |
| C3 | **Window features for stratified sets.** Compute per window: realised volatility, spread, top-of-book depth, final distance between reference and K, time of day. Allow filters and balanced splits on them. | The recipe refuses volatility, spread and liquidity filters today; strategies behave differently by regime | M–L |
| C4 | **Sample-size guidance.** Before running, estimate entries and fills from earlier runs on similar sets; after running, show a confidence interval for P&L per fill and say when a result is too small to compare. | The 23-window runs gave 8 and 3 fills; the numbers mostly reflected chance | S–M |
| C5 | **Growing sets with versions.** When new recordings arrive, create "version 2" of a set with the new windows, keeping version 1 immutable and both linked. | Recordings will keep coming; sets are immutable by design | M |
| C6 | **User-chosen reserved windows for Tune.** Mark a recording or date range as held out; Tune uses it as its holdout and Replay warns before using it. | Prepares Phase 3 without re-introducing a hidden split | S–M |
| C7 | **Export set membership** (CSV or JSON with window ids, times, coverage profile) for notebooks and external checks. | Notebooks should read what the screen saves | S |
| C8 | **Recording health page.** Per recording: disconnects over time, reference lag, metadata freshness, tick changes, provider resolution delay. | Data problems are found today only through replay results | M |

## Corrections to the current policy found during this work

- In a 5-minute window, a single hole over 30 s already breaks the 90% rule, so the 30 s cap never binds. Options: lower it to 20 s, or keep it only for longer windows. Decision needed.
- `exploratory` sets of 90% and 99% are still labelled `exploratory95_v1` in some places; the label should carry the stored floor.

## Suggested order

1. Quick wins that remove confusion: B1, B4, C1, A2 (and the 30 s decision).
2. Speed: A1, A3, A4, A5.
3. Understanding the data: B2, B3, B5, B6, C8.
4. Research depth: C3, C4, C2, C5, C6, C7.
5. Operational, in parallel: A8 (recorder), A6 (reconstruction).

## Implemented, 23 October 2026

Approved for implementation: A1–A5 (backend speed), A8 plus recording from the page, B1–B4 (screens), C1, C3, C4, C5, C8 (research needs), and the 20 s hole cap. Not implemented: A6 (reconstruction blips), A7 (re-normalization), B5–B8, C2, C6, C7.

| Id | What shipped | Measured |
|---|---|---|
| Policy | `replay_research_v2_1`: hole cap 20 s, default in the Data Library; `replay_research_v2` stays readable | Keeps 95 of the windows v2 kept 104; the 23-window 6 Oct set becomes 20 |
| A1 | Inventory cached per warehouse, on disk next to the warehouses, keyed by build and quality stamp; filter-free; a recording warehouse refreshes at most every 10 min | Data page after a restart: 185 s → 4 s |
| A2 | Pin digests and membership receipts persisted; changed files are re-hashed | Opening a set in a fresh process: 9.5 s → 1.7 s |
| A3 + A4 | Worker keeps one catalog and the last window per process; units grouped by window; pool of 3 processes (`TYREX_REPLAY_WORKERS`) | 46 units in 596 s; Edge 3c decision digests identical to the sequential run on 23/23 windows |
| A8 | Root cause: the collector subscribed every market discovered 24 h ahead and never unsubscribed; the socket grew to ~700 tokens and was closed every few minutes. Books are now subscribed 15 min before a market opens (`coverage.book_subscribe_ahead_s: 900`), ~10 tokens at a time | Not yet confirmed live: Polymarket is unreachable from the current network (TLS rejected), see below |
| Recording | `recording.plan/start/status/list/stop` and the Record panel: duration, warehouse (new folder or existing), name, live normalization, network and credential checks, disk estimate, live progress and clean stop. Places no order. | A start is refused while Polymarket is unreachable |
| B1 | "Windows under the current policy": kept and excluded per hour with reasons, same rule as preview and save | 88 of 204 on 6, 15, 19 Oct |
| B2 | Window map on the warehouse view and in the set preview; click opens the window | |
| B3 | Recording cards: windows kept, book time lost, "gaps every window from HH:MM" | 15:45, 17:00, 13:05 |
| B4 | Raw check boxes hidden under research policies; the Scope preview button only lists windows to pick | |
| C1 + C5 | `selection.derive`: re-save under current rules and new versions with more recordings, as linked sets (`parent_selection_id`, `version`) with the window difference shown before saving | |
| C3 | `library.features` and condition filters in custom sets: reference volatility, move from K to close, mean spread, share of time wider than one tick (quotes carry no sizes, so depth is not offered) | 61 s once for all recordings, then 0.9 s |
| C4 | Results: qualified P&L per fill with a 95% interval and a size verdict; preview: expected entries and fills from earlier runs under the same scenario | |
| C8 | Recording health: book seconds lost per window per recording, with the onset of continuous gaps marked | |

Also fixed on the way: a Research v2.1 preview crashed (a variable reused for the policy shadowed the source choice), now covered by an end-to-end test; Windows `PermissionError` when reading a file being replaced (status polling) now retries.

Network note: from the current network `gamma-api.polymarket.com` resolves to an OVH address and TLS verification fails for every Polymarket domain, while Binance works. Earlier recordings were made from a network where Polymarket was reachable. The recorder fix needs a live run from such a network to be confirmed.


## Remaining proposals implemented, 23 October 2026 (second pass)

| Id | What shipped | Measured / verified |
|---|---|---|
| A6 | Scenario field `book_reconstruction`; new scenarios `friction_v3` (default) and `friction_stress_v2` rebuild books with `venue_top_v1` in both the strategy feed and the simulator. `friction_v2`/`friction_stress_v1` and older stay unchanged for reproduction. | On 3 windows: legacy had 124, 314 and 144 crossed-book invalidations, `venue_top_v1` none; real disconnects kept. Same P&L on 3 filled windows, 0 blips to tolerate (29, 76, 8 under v2). Coverage reports in the warehouses were computed with legacy books; the 250 ms blip tolerance of the set policy covers them. |
| A7 | `PARSER_VERSION` 2: metadata minimum order size recorded in shares. Readers use version-2 builds only; version-1 builds and their files stay on disk, so saved sets keep reading their pinned builds. All four warehouses re-normalized. | Phase B: 302 segments in 10 min, minimum size reads "shares" in all 1,979 rows. Studies saved before the change are replayed on the parser-1 builds they used, with a note saying so (notebook 06 bundles open again). |
| B5 | Set card details: name and description (edit history kept; windows, pins and digest unchanged), versions, runs that used the set with links to their results, candidates from those runs, last used, mini window map | |
| B6 | Data-only window view in the window drawer: Up and Down best bid/ask and the reference from 5 min before the open to 30 s after the close, holes shaded, reconstructed K and the provider price-to-beat | |
| B7 | Trash shows size and age per entry and in total; "remove entries older than 30 days" | |
| B8 | One style scope for the whole application (Data and Research) | Checked light and dark |
| C2 | `selection.lineage` (ancestors, children, runs, candidates); opening a run from a set jumps to Research results | |
| C6 | `reservation.save/list/delete`: windows reserved as a Tune holdout; warnings in the set preview and the Replay preview; nothing is removed silently | |
| C7 | `selection.export` CSV/JSON (header with policy and digest; per-window times, coverage, gap profile, price-to-beat); download from the set card, copy kept under `exports/` | |

Also: an index refresh is limited to every 10 minutes while a warehouse is being written (recording or normalization), so the Data page stays responsive during both.


## Follow-up after the first workspace recording, 23 October 2026

| Point | What shipped | Verified |
|---|---|---|
| Replay command windows | Background workers (replay jobs, preparation, recording) start with a hidden console that their pool processes inherit, instead of `DETACHED_PROCESS`, which gave every pool process its own visible window. A pool process that dies now fails the job with `replay_worker_ended` (finished windows kept). Failed jobs offer Resume. | A 2-worker replay opened no window. Control: the old flags open a Windows Terminal window and the new ones do not. |
| Recording closing | `--finish-windows` records until the window open at the planned end closes, plus 30 s. `--outcome-wait` then stops books and feeds and polls only resolution and metadata until every recorded window has its winner, or the wait ends (default 15 min, at most 60). The session file records `windows_until`, `capture_ended_at` and `outcome_wait`. The library assigns windows only up to `windows_until`. A window cut by a stop is excluded as "recording ended before the window closed" (relabel only: kept counts unchanged on all older recordings). | Collector tests for finishing, the wait limit, a stop while finishing, and winners-only sessions. The live recording path still needs one real run. |
| Fetch missing winners | `recording.fetch_outcomes` resumes the recording's collection in winners-only mode (`--outcomes-only`). It asks the resolution endpoint about complete windows without a winner and stores the answers as observations received now. It records no window of its own. Recording health shows the count and the button. | 23 Oct recording: 1 missing (16:05); 15 Oct recording: 2 missing. Not run from here. |
| Set cookbook | Recipes are versioned files in `config/research/set_recipes/` (`us_open_hour`, `high_volatility`). The Goal step shows them as cards; parameters are edited on Scope. Percentile bounds become absolute thresholds at preview time and are saved with the set, with a copy of the recipe. Derive re-ranks over the new recordings. A custom set's filters can be saved as a new recipe; outcome filters are refused. Missing inputs is no longer offered as a set; windows with missing inputs show per recording. | High volatility on all recordings: threshold 1.84 bp (67th percentile of 97 windows), 33 kept. US open hour: 12 windows matched, none passes the book policy. |
