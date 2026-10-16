# Phase B — operator validation commands

## Rescore the existing 45-minute archive after the coverage correction

Stop any older offline `quality` command on this same root with Ctrl+C first.
No new recording is needed. In Git Bash:

```bash
cd /e/polymarket/Tyrex_PM
./.venv/Scripts/tyrex-data.exe quality --root var/warehouse_phase_b_45_test --collection 7af1f9df48ee46f5aa3e049342185af2
./.venv/Scripts/tyrex-data.exe status --root var/warehouse_phase_b_45_test --collection 7af1f9df48ee46f5aa3e049342185af2
```

The current index is `coverage-index-v2.sqlite`. Once no older quality process is
running, the earlier `coverage-index-v1.sqlite` (about 9 GB on this archive) is unused
and can be deleted. The first index build scans historical normalized files; later
runs reuse committed parts. `quality_progress` distinguishes indexing, individual market publication and
completion. Quality publication does not replace the full `verify` checksum walk.
Legacy session receipts do not retroactively acquire the new shutdown flags; null
flags on the old recording are expected. Future runs distinguish capture completion
from the final synchronous derived-data drain.

Updated 1 October 2026. These are the tests the operator will run. The coding pass does not perform live capture, change `.env`, disable TLS verification or place orders.

## 0. Five-minute connectivity check — done 1 October 2026

```powershell
.\.venv\Scripts\run_data_collection_live.exe --profile btc-5m --root var/warehouse_phase_b_quick --duration 5m
```

Collection `9fcb456e9a274587b645a30ac44b9da4`, session `2d77b56135094ef58d79edcf9cd27d7f`. Clean stop. All 178,748 received messages were fsynced. CLOB, Gamma, resolution, PolyBolt, and Binance all produced files, and the CLOB clock status was `OBSERVED`. Keep this root as the connectivity sample.

## 0b. Forty-five-minute capture — recorded 1 October 2026

```powershell
.\.venv\Scripts\run_data_collection_live.exe --profile btc-5m --root var/warehouse_phase_b_45_test --duration 45m
```

Collection `7af1f9df48ee46f5aa3e049342185af2`, session `88ea0e2cc3194eeb8eba4dac96608218`, 07:42:08–08:27:14 UTC. Clean stop. `received = appended = fsynced = 2907353`. Unknown messages, parse failures, and drop counters were empty. The CLOB clock was `OBSERVED`.

Five windows finished after a full 15-minute warm-up: 08:00, 08:05, 08:10, 08:15, and 08:20 UTC. Each has both outcome-token book snapshots from about 07:43 through the window. Reference ticks and Binance trades span the session with no gap longer than 30 seconds. PolyBolt stayed connected. Binance reconnected 4 times and the CLOB reconnected 27 times; measure those holes before calling book coverage complete. The quality worker’s last publication was about 17 minutes before shutdown and still says `normalization_pending`, so it is not the final readiness score. Run `normalize`, `quality`, and `verify` on this collection before the restart gate. Semantic readiness remains `UNSUPPORTED`.

## 1. Validate configuration, then record a smoke session

Run from PowerShell in the repository. Existing `.env` credentials are loaded automatically; no need to copy or display them.

```powershell
Set-Location 'E:\polymarket\Tyrex_PM'
.\.venv\Scripts\run_data_collection_live.exe --profile btc-5m --validate-config
.\.venv\Scripts\run_data_collection_live.exe --profile btc-5m --root var/warehouse_phase_b_validation --duration 45m
```

Save the `collection_id` printed at startup. Configuration success proves local configuration and credential presence, not provider access. A clean exit proves shutdown, not market completeness. The previously observed Polymarket TLS hostname error must be resolved through the correct network/provider configuration; leave certificate verification enabled.

In a second terminal, replace the placeholder once:

```powershell
Set-Location 'E:\polymarket\Tyrex_PM'
$collectionId = 'PASTE_COLLECTION_ID'
.\.venv\Scripts\tyrex-data.exe status --root var/warehouse_phase_b_validation --collection $collectionId
```

Check writer health, queue drain, feed connections, both outcome subscriptions, unknown/rejected messages, dropped messages, normalization lag and the latest quality publication time. Required feeds are Polymarket books, the official reference feed and Binance. Three complete consecutive windows must remain after the 15-minute warm-up. A Binance-only recording does not pass.

## 2. Exercise a clean restart

After the first command has stopped, resume the same collection:

```powershell
.\.venv\Scripts\run_data_collection_live.exe --resume $collectionId --root var/warehouse_phase_b_validation --duration 30m
```

Confirm a new session ID, new book seeds, preserved raw IDs and pending resolution work, and visible separation between sessions. Do not run two collectors against the same root. Forced-exit durability and uncertain restart bounds are covered by automated tests; an operator crash exercise should use a separate disposable recording, not terminate the trading process.

After stopping:

```powershell
.\.venv\Scripts\tyrex-data.exe normalize --root var/warehouse_phase_b_validation --collection $collectionId
.\.venv\Scripts\tyrex-data.exe quality --root var/warehouse_phase_b_validation --collection $collectionId
.\.venv\Scripts\tyrex-data.exe verify --root var/warehouse_phase_b_validation --collection $collectionId
.\.venv\Scripts\tyrex-data.exe list-markets --root var/warehouse_phase_b_validation --collection $collectionId
```

The collector normally manages normalization and quality workers itself. These one-shot commands are an explicit final drain and report, not extra concurrent `--follow` workers.

## 3. Export and inspect a completed market

Choose an observed `market_id` from the listing, then use a new output directory:

```powershell
$marketId = 'PASTE_MARKET_ID'
.\.venv\Scripts\tyrex-data.exe verify --root var/warehouse_phase_b_validation --collection $collectionId --market $marketId --require-ready
.\.venv\Scripts\tyrex-data.exe export-market --root var/warehouse_phase_b_validation --collection $collectionId --market $marketId --output var/research/exports/phase-b-smoke-001
```

`--require-ready` should fail while official rules/thresholds, payout units or status enums remain unsupported. Do not invent an attestation to clear it. Inspect `quality.json` for separate integrity, normalization, reconstruction, semantic and settlement results. `source_context.json` pins the effective profile and provider policy. Earlier seed/bridge evidence is included with original observation times; delayed outcome labels are separate.

The whole collection includes future windows from the 24-hour discovery lookahead. Qualify selected completed windows individually rather than expecting every discovered future market to pass.

## 4. Run the soak and follow selected resolutions

After reviewing the smoke results together:

```powershell
.\.venv\Scripts\run_data_collection_live.exe --resume $collectionId --root var/warehouse_phase_b_validation --duration 24h
```

This crosses UTC midnight. Preserve the root, session logs, quality publications and verification reports. Check bounded memory/queue behavior, stable processing lag and disk runway throughout. Resume afterward when selected markets still need resolution or 1-hour/24-hour follow-up audits. Unresolved obligations are not discarded on shutdown.

## 5. Synthetic throughput benchmark

This command creates synthetic data only; it uses no venue connection or credentials. Use a new root for every run. Run it separately from the live soak unless intentionally testing concurrent disk contention.

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_warehouse.py --root var/warehouse_phase_b_benchmark_final --seconds 1800 --rate 1500
```

Adjust `--rate` to at least twice the newly observed peak message rate. Inspect `benchmark_report.json`: counts must reconcile, no writer failure, receipt-to-fsync p99 at most 2 seconds, and normalization lag at most 120 seconds after startup. Memory samples must be present before claiming a memory result. The benchmark labels its scope: synthetic Binance trades, writer-process CPU, Windows writer/normalizer working sets and the last 10,000 durable-latency samples. It is not a mixed-feed soak or a proof of millisecond end-to-end provider latency.

An earlier 30-minute benchmark exists under `var/warehouse_phase_b_load_30m`, but predates the revised sealer and quality workers. It does not qualify the final implementation.

## Evidence to share

- Collection ID, selected completed market IDs, startup/stop times and any error classes.
- `collections/<id>/status.json`, `quality_status.json`, session worker logs and saved verification reports.
- One complete export directory and the benchmark report.
- Provider evidence needed to settle the unresolved facts in `provider_contract_report.md`.

Do not share `.env`, API secrets or authentication frames. Raw market data and the saved profile do not require wallet credentials.
