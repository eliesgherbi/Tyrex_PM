# Data Library backlog

> **History.** Superseded by [STATUS.md](STATUS.md) and `Docs/latest/`. Kept as the record of how this was decided.

Date: 21 October 2026. Status: DL-1 through DL-5 are implemented and checked on fixtures, with a live-warehouse browse. The workspace lists five finished capture sessions across four warehouse roots. A window belongs to a recording only when its start falls inside that capture. Exploratory coverage at 95% saved 15 windows from the 6 October recording; complete coverage on that recording includes 0. The saved exploratory set is `7470720940814968bac80839d60e1636`. The older partial-coverage set `a623f7b6d3cc4525b5310d3613072020` is unchanged and is not the research path. A collection-wide quality run returned the existing publication and did not assess the 280 windows that start after `2026-10-06T19:36:34Z`. This is not Data Library acceptance and does not qualify live trading. `safe_stop` remains `unsupported`. The 20 October evidence stays in [step_03_implementation_record.md](step_03_implementation_record.md).

Do not edit `prototypes/ihm-review/` as part of these increments. Bind a screen only after the increment named below has passed. Do not launch a collector, a paper session, or an order.

## Smallest slice

DL-1 is the first backend increment. It is the warehouse view: paged windows, coverage, and one window's tracks, with no writes.

The smallest path that reaches replay is DL-1, then DL-2, then DL-3, using windows whose required tracks are already `present`. DL-4 is required only when the preview says preparation is still needed. P4's live recording session, P5 launch, and P6 capture are not on this path.

## Increments

| Id | Depends on | Work | Done when |
|---|---|---|---|
| DL-1 | `Reader`, `WindowCatalog`, committed manifests, published quality | `library.windows`, `library.coverage`, `library.window`. UTC half-open window-start bounds. Seven tracks plus warm-up on the row. Availability filter. Read dispatch does not create the workflow root. | Fresh process: list, timeline, and detail agree for one complete and one incomplete window. Archive and workflow directories are unchanged. |
| DL-2 | DL-1 | Real manifest pins and warm-up dependencies. `custom_window_set` filters: outcome, label source, daily hours, IANA zone, weekdays, `all_matches` or `picked`. `eligible_windows` and `missing_inputs`. `selection.save` requires `preview_id` and stores the filters. Empty catalog and `legacy_unpinned` refusals. | Saved membership equals the reviewed preview and reopens with the same filters. Overnight, Paris, and DST cases match the server rules. A reserved market is withheld before outcome counts. A changed pin is `pin_mismatch`. |
| DL-3 | DL-2, existing `strategy_replay` worker | `experiment.bind` from a selection id. Server-built units. Baseline, metric definitions, and acceptance criteria stored before launch. `max_units` applied before expansion. `missing_inputs` refused. Outcome-conditioned sets keep that limitation. | One recorded selection runs without units in the request. CLI and notebook report the same plan digest and manifest checksums. |
| DL-4 | `normalize_once`, `publish_quality`, `verify`, OS lock | `preparation.plan/start/status/stop`. Collection-wide scope stated. `owner_busy` for collector and owner locks. `safe_stop: unsupported` until the owner observes a stop at a segment or build boundary. | Isolated incomplete fixture gains derived output. Raw observations stay unchanged. A second writer is refused. No collector starts. |
| DL-5 | The increment being bound | Thin transport over `workflows.call`. Same envelope for CLI, notebook, and frontend. | The bound screen shows that operation's ids, counts, and errors. Browsing still writes nothing. |

## DL-1 contract

Request fields shared by `library.windows` and `library.coverage`: `roots`, optional `collection`, `session_id`, `asset`, `duration`, `start_utc`, `end_utc`, and optional `schedule`. Pages add `limit` and `cursor`. Coverage adds `bucket_s`. Detail adds `warehouse`, `collection`, and `market_id`.

Result fields: `timezone: UTC`, `interval: half_open_window_start`, `aggregation: count_of_windows_by_start` on coverage, `assessed_at`, publication ids, `unavailable_roots`, and `next_cursor` on pages. Each track is `present`, `missing`, `not_expected`, `unassessed`, `unknown`, `withheld`, or `unavailable`. Unknown is its own count.

`schedule: btc_updown_5m` is the only request that may add expected-but-unrecorded slots. Other requests do not invent them.

Reserved markets return `outcome: withheld` and omit the resolution value. `provider_confirmed`, `proxy`, and `unknown` stay distinct.

## Later contracts, in order

DL-2 filter object: `outcome`, `label_source`, `daily_time`, `weekdays`, `time_zone`, `selection_mode`. Preview result: filter candidates, included, excluded with reasons, outcome-mix counts that omit withheld markets, ambiguities, `preparation_needed`, `population`, `preview_id`, and `inventory_fingerprint`. `volatility`, `spread`, `liquidity`, and `reversal` return `unsupported_measurement`. Save without the current preview id returns `preview_stale`. Reusing stored filters calls resolve again and creates a new set.

DL-3 request: `selection_id`, `strategy_kind`, parameters or configurations, execution-scenario ids, `baseline`, `metrics`, `acceptance_criteria`, `max_units`. No `units` and no `pin_parts`. Units written by the backend are `strategy_replay`. Caller-built experiments remain `units_source: caller` and are not this path. `missing_inputs` returns `diagnostic_not_replayable`. `outcome_conditioned` is stored on the experiment.

DL-4 plan result: owner function, lock roles, `scope: collection`, and `safe_stop`. Verify must not publish or write builds. Normalize and quality stay collection-wide even if the recipe dates are narrower.

## Out of this backlog

- Rewriting historical selections or result bundles.
- A new storage engine or an index rebuilt by a read.
- Per-window pause and resume.
- Paper launch, host restart, and ask70 capture.
- Proving P4 against a live collector session. That proof remains P4's own gate.
