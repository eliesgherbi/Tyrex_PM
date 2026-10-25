# Data warehouse

The warehouse is a separate collector and research reader. It does not start the trading host, submit orders, or load strategy plugins. Keep its storage root apart from any trading run directory. Live capture and the 24-hour soak are still operator gates; see [provider contract report](../implementation/pm_data_warhouse/provider_contract_report.md).

## Install

```text
python -m pip install -e ".[warehouse]"
```

That extra pins `httpx==0.28.1`, `pyarrow==23.0.1`, `duckdb==1.5.6`, and `zstandard==0.25.0`. Trading commands stay usable without it.

When the BTC profile requires the reference feed, set `TYREX_DATA_PM_API_KEY`, `TYREX_DATA_PM_API_SECRET`, and `TYREX_DATA_PM_API_PASSPHRASE`. A `POLYMARKET_*` triplet is accepted as a fallback, including `POLYMARKET_PASSPHRASE`. The collector records that the names were present. It does not write the values into the profile, status file, or export.

## Validate and record

```text
run_data_collection_live --profile btc-5m --validate-config
run_data_collection_live --profile btc-5m --root var/warehouse --duration 45m
```

`--profile`, `--config`, and `--resume` are mutually exclusive. `--validate-config` checks the profile and credential presence offline. It does not open a provider connection. The built-in profile is the package resource `btc-5m`; `config/data_collection/btc_5m.yaml` is the documented example.

The record command prints `collection_id` and `session_id`, then runs until `--duration` elapses or the process is interrupted. Normalization and periodic quality publication start in separate supervised child processes unless you pass `--no-normalize`. A failed worker is restarted with bounded backoff; logs are saved under the collection’s `sessions/` directory. Omit `--duration` for a continuous session. A clean stop exits 0 even when semantic quality is still `UNSUPPORTED`. Disk-floor or queue overflow exits 3. A clean capture whose final derived drain failed exits 5.

Forty-five minutes is a smoke length. Count complete windows in the quality report. The default warm-up is 15 minutes.

## Status, resume, normalize

From another terminal, while the collector is running:

```text
tyrex-data status --root var/warehouse --collection <collection-id>
```

The heartbeat includes feed health, expected-window counts, pending tasks, `received` / `appended` / `fsynced`, queue size, `oldest_unflushed_age_s`, unknown messages, parse failures, drop counters, recent gaps, clock, raw bytes, free disk, projected days to the disk floor, and `semantic_readiness`. A fresh heartbeat does not make a market semantically ready. The collection-level semantic indicator has no market fingerprint and stays `UNSUPPORTED`; inspect a market report for a market-specific decision. The heartbeat includes compact normalization and quality-worker health.

```text
run_data_collection_live --resume <collection-id> --root var/warehouse
tyrex-data normalize --root var/warehouse --collection <collection-id> --follow
tyrex-data quality --root var/warehouse --collection <collection-id> --follow
```

Resume loads the saved profile and opens a new session. A profile-hash change needs a new collection. An unclean previous stop is recorded as `session_interrupted`. Resume does not backfill an interval that was never received. Standalone `normalize --follow` and `quality --follow` are for a capture started with `--no-normalize`; run them in separate terminals. Do not start another copy when the collector already manages them. A second owner of the same root is rejected.

Only fsynced records are durable. Recovery replays sealed segments and complete lines in an open `.open.jsonl` tail. A torn last line stays visible and is skipped.

## Verify, list, export, compact

```text
tyrex-data verify --root var/warehouse --collection <collection-id>
tyrex-data verify --root var/warehouse --collection <collection-id> --require-ready
tyrex-data list-markets --root var/warehouse --collection <collection-id>
tyrex-data export-market --root var/warehouse --market <market-id> --output var/research/exports/<export-id>
tyrex-data compact --root var/warehouse --collection <collection-id>
```

`verify` checks archive checksums and normalization coverage. Exit 0 means the archive matches its manifests. `--require-ready` uses readiness predicate version 2 (`quality/readiness.py`), evaluated on quality reports of version 4 (`QUALITY_VERSION`): verified archive integrity, complete normalization, usable required-feed intervals including warm-up, observed closure, supported semantics, provider-terminal settlement and an unambiguous payout mapping are all required. Use `--market <market-id>` to qualify a selected completed window; the whole collection includes future and incomplete windows and will normally fail that stricter gate. An empty root fails verification. With the default policy, that flag returns 1 because the official threshold is unattested.

`export-market` writes an immutable bundle: observation parquet, derived `boundary_observations`, `book_epochs`, `resolution_assessments`, `coverage_intervals` and `market_coverage`, plus `quality.json`, `boundaries.json` and `source_context.json`. The context pins the effective profile, source-contract version, provider policy and archive evidence. Earlier snapshot/bridge dependencies are included, with original reception times, so the book can be rebuilt at the export boundary. Post-interval metadata cannot extend the decision horizon; outcome labels remain separate. An existing output directory is rejected. `created_at` is the window horizon, and boundary `computed_at` is that same horizon. Compaction streams input parts, bounds each output by a row cap and the configured size target, supersedes older builds and records logical checksums. A single large record or input batch can exceed the soft byte target; memory does not grow by loading the entire collection. Pinned old readers remain valid. It does not delete raw segments.

Read quality this way:

- Periodic reports leave `capture_integrity` as `MANIFEST_VERIFICATION_REQUIRED`; explicit verification and exports perform checksum checks and mark it `VERIFIED` or `FAILED`. A periodic health report alone is never a research readiness approval.
- `semantic_readiness` is `UNSUPPORTED` until `collections/<id>/provider_policy.json` sets `rule_status` to `SUPPORTED`, `official_threshold` to `ATTESTED`, and lists that market's rule fingerprint. The only payout unit is `USD_per_share`. Scoring uses `payout_index_basis: outcome_index` when you set it.
- Reference gaps follow connection/session evidence, sequence failures and restarts. An unclean session stops at its last confirmed evidence; it is not extended to the restart timestamp. A five-second stale tick is a warning, not a missing interval.
- CLOB books reconstructed from websocket snapshots are `UNKNOWN_CONTINUITY`. REST audits stay beside that book.
- Empty or contradictory resolution stays unresolved. A later contradiction does not erase the earlier observation.

## Replay

Pin each export by the sha256 of its `manifest.json`:

```json
{
  "schema_version": 1,
  "exports": [{"path": "exports/example", "manifest_sha256": "<sha256>"}],
  "selection_policy": "operator_note",
  "expected_windows": 1,
  "discovered_windows": 1
}
```

Paths are relative to the dataset file. Then:

```text
tyrex-research replay --dataset var/research/datasets/btc5m-study.json --scenario config/research/replay_conservative.yaml --output var/research/experiments/study-001
```

The example scenario is in `config/research/replay_conservative.yaml`. Intents are a JSON list (`order_id`, `at_ns`, `instrument_id`, `action`, `quantity`, `limit_price`). Replay reads the pinned export only. It does not open the network. The same dataset, scenario, and intents produce the same logical result.

Fills use the reconstructed book at simulated arrival. Default maker mode is `no_fill`. Participation and fee numbers in the example file are scenario assumptions. A payout label is applied after the decision loop and does not fund an earlier order. Unresolved markets stay in `dataset_counts.unresolved`. `dataset_counts.traded` counts windows with at least one fill; `filled_orders` and `fills` are reported separately. `strategy_ready` uses exactly the same full quality predicate as verification. The looser `valid_books_at_actions` mode is explicitly scenario research; it does not certify a dataset. Intents must fall within exported decision intervals. Qualified payout scoring is retrospective: it creates receivables and never recycles settlement cash into earlier trades.

## What a recording is for

Use exports to count discovered windows, inspect books and reference coverage, and run scripted what-if orders. Settlement-value and residual-band research still need an attested rule and threshold before a window is semantically ready. Strategy adapters are outside this module.

Closed-loop strategy replay lives in `research/strategy_replay` (it imports the warehouse reader; the warehouse imports nothing from it). It reads one window directly from a collection with time-bounded reads, through a `WindowCatalog` that scans market metadata, instruments and resolutions once per collection. Its outcome labels are **proxy** labels (single positive payout entry, payout unit still `UNKNOWN`), recorded as `label_type: proxy_outcome`; it does not change readiness or attestation. The scripted `tyrex-research replay` path is unchanged and rejects closed-loop scenarios.

In `recorded` fee mode, the replay engine accepts both observed fee-schedule shapes, CLOB `{"r","e"}` and Gamma `{"rate","exponent"}`. A Gamma row previously dropped the curve and caused `recorded_fee_curve_unavailable`.


## Periodic reports and recovery

The quality worker attempts publication every `operations.quality_interval_s` (30 seconds by default), using committed normalized builds. Work is isolated from raw capture. Reports for completed windows are reused until relevant metadata, resolution, policy or capture inputs change; their original assessment time and input build IDs remain visible. Future windows stay explicitly not started. Full export/verification computes a fresh assessment, including later evidence.

Immutable publications are under `collections/<id>/quality/`, with reusable market artifacts under `quality_markets/`. Publication manifest file paths are relative to the warehouse root. `quality_status.json` points to the latest completed publication. Check its timestamp during the live test: worker health does not prove the workload meets the cadence target.

After stopping a collection, these commands finish any outstanding offline work:

```text
tyrex-data normalize --root var/warehouse --collection <collection-id>
tyrex-data quality --root var/warehouse --collection <collection-id>
tyrex-data verify --root var/warehouse --collection <collection-id>
```

The sealer preserves the original journal until compressed publication commits. On a storage failure, stop intake, restore disk space or access, then resume the same collection. Do not delete raw segments, quarantine files, old build manifests or provider evidence. Compaction does not perform retention/deletion.

Provider policies are operator-supplied evidence, not an automatic verification service. Do not mark a policy supported to make a test pass. A semantic flag does not supply a numerical threshold to a strategy. Official boundary candidates remain separately labeled; constructing the new strategy’s probability inputs remains outside this collector.

See the [live validation commands](../implementation/pm_data_warhouse/operator_validation.md) for the operator-run smoke, restart, soak and load checks. Recordings of 20 minutes to 6 hours were made in October 2026 (`var/warehouse*`); the 24-hour soak is still ahead.

## What lands on disk

One recording root holds one or more collections. A collection is one profile, one or more sessions.

```text
var/warehouse/
  spool/          open journals while a file is still being written
  raw/            sealed original messages, one folder per source
  normalized/     typed tables built from those sealed files
  builds/         manifests that name each normalized batch
  catalog.sqlite  small index rebuilt from the journals
  collections/<id>/
    collection.json     the profile used for this recording
    status.json         latest heartbeat
    sessions/           start, stop, and worker logs
    quality/            periodic coverage snapshots
```

A message is counted three times: received into memory, appended to the open journal, then fsynced. Only the fsynced copy is durable. After the journal rotates, a sealer compresses it into `raw/` and a normalizer writes typed Parquet under `normalized/`. Quality snapshots describe coverage. They do not replace `tyrex-data verify`.
## Coverage index and shutdown

Quality policy version 4 uses a disposable coverage index per collection,
`collections/<id>/coverage-index-v2.sqlite`. Each new immutable normalized part is
checksum-checked and indexed once, in a transaction. Rows are stored as compressed
blocks per dataset and instrument, so the index stays about the size of the
normalized archive. The index records its layout and field projection; a code
change to either, or a changed/compacted input set, rebuilds it.

CLOB books are rebuilt per socket stream (instrument, session, connection). A
socket's updates apply only to that socket's own snapshot; an update on another
socket no longer erases the book. An `unsubscribe` closes that socket's copy. An
outcome book counts as usable while any of its streams is usable. The collector
keeps each token on the same socket while it stays active, so market rolls no
longer move tokens between sockets; the per-stream rule covers any remaining
overlap and recordings made before this change. Policy 4 reports are not reused
from policy 3 caches.

Market reconstruction reads the market's two outcome books from the latest real
seed before warm-up of every stream that is still open. Binance is seeded only when a connection starts, so one causal
Binance replay serves every window of a publication or verification instead of one
replay per window. Operational and reference rows are decoded once per reader.
Completed windows are processed before active/future windows. Their reports are
reused when the relevant input builds, rule, boundaries, outcome mapping, settlement
evidence, and policy have not changed. Required feed coverage ends at window expiry;
later settlement polling does not extend that trading interval.

Only the quality publisher builds the index and takes its lock. `verify` and
`export-market` never write or lock it: they use it read-only when it exactly
matches the committed normalized parts, and otherwise read the archive directly,
which gives the same result more slowly. Run `tyrex-data quality` before a large
`verify` to refresh it. An earlier `coverage-index-v1.sqlite` from the first
version of this correction is no longer read and can be deleted.

`quality_progress.json` reports indexing progress and the latest published market
report while the full publication is still running. `quality_status.json` continues
to name the last complete immutable publication. `tyrex-data status` reads both.
The first index build can take minutes for a multi-million-message archive; this is
offline work, not a trading-loop operation. Subsequent updates ingest only new parts.

Shutdown stops the workers, finishes normalization of sealed segments, then waits
for a final quality publication. On a long collection this final drain can take
minutes. Session receipts and the stop response distinguish `capture_clean` from
`derived_processing_complete`. `clean_stop` retains its legacy capture meaning. A
failed drain leaves `derived_processing_complete=false` and an error class; the
recording command then exits 5, while raw capture can still be clean. Exit 3 stays
reserved for raw durability failures. Completion of processing is not a claim of
semantic readiness or archive checksum verification. With `--no-normalize`,
derived completion stays false until the operator processes the archive separately.

Do not run an additional quality worker concurrently against the same root. A
locked-root error means the existing worker must finish or be stopped before the
new one-shot command can publish. Raw data does not need to be recorded again.

## Parser versions, market families and research replay

- **Parser version 2** (`data_warehouse/contracts.py:PARSER_VERSION`) records the metadata minimum order size in shares. Readers use builds of the current parser version only. Builds of version 1 stay on disk so window sets and studies pinned to them still open and replay; this roughly doubles the normalized footprint until those sets are retired.
- **Market families** come from `config/markets/` (`domain/polymarket/window_families.py`). A collector profile names an asset and a window length; the warehouse family, slugs and window bounds follow from the matching family. The normalizer recognizes enabled families only, so enabling one goes with a parser version bump.
- **Scenarios** for strategy replay are in `config/research/scenarios/`: `friction_v3` (default) and `friction_stress_v2` rebuild books with `venue_top_v1` (reconciled with the venue's reported top of book); earlier scenarios stay for reproduction. The scripted replay above (`tyrex-research replay`) is the simulator's own harness; strategies are replayed through `research/strategy_replay` and the research workspace ([research_workspace.md](research_workspace.md)).

