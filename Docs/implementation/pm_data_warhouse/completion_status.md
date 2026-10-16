# Phase B completion status

## Final review corrections — 3 October 2026

- Token-level `market_changes` (for example `tick_size_change`, keyed by instrument)
  were normalized but dropped by market queries and exports, so replay could keep
  an outdated tick. Exports and `read_observations` now match them.
- `raw_bytes_per_day` and the disk runway use only the current session's growth;
  after a resume they no longer divide the whole archive by the new session's uptime.
- Replay `dataset_counts.traded` counts distinct windows with a fill; `filled_orders`
  and `fills` are reported separately.

The 5-minute resume of `2dd3d0d125dc4a9fb5e50daf3f9bfd3e` created a second session
cleanly (received = fsynced, no normalizer restart). Tests: 547 passed, Ruff clean.
Implementation is complete against the Phase B plan; the soak, benchmark, `clob-0`
observation check and provider attestation remain qualification work.

## Socket overlap and Windows rename correction — 2 October 2026

The 45-minute validation recording (`2dd3d0d125dc4a9fb5e50daf3f9bfd3e`) verified
clean (`integrity_ok`, 2,291,577 raw = processed). Of nine finished windows, one
started before the recorder, six were 99.87–99.98% (sub-second `crossed_book`
holes), and one was 62%: about 110 s of `connection_without_seed`.

Cause: tokens were split across the two CLOB sockets by sorted position, so each
market roll moved boundary tokens to the other socket and both briefly streamed
them. Reconstruction then let one socket's updates erase the other's snapshot.

- Collector: sticky token-to-socket assignment (`assign_shards`).
- Reconstruction: per-socket CLOB streams, unsubscribe closes a stream, coverage
  is the union of streams. Quality policy 4, reconstruction version 3.
- The normalizer crash was a Windows sharing violation renaming
  `normalization_status.json` while a reader had it open. Atomic publication now
  retries that rename briefly.

Tests: 544 passed, Ruff clean. The recording has not been rescored with policy 4.
Sub-second `crossed_book` holes are unchanged and still make those windows `GAP`;
whether they are real venue quotes is not yet investigated.

## Index size and read-path correction — 1 October 2026 (later)

Review of the first correction found four problems, now fixed offline:

- **Index size.** The per-row index for the 45-minute capture was 9.3 GB against
  484 MB normalized (repeated text order keys in the table and two indexes); a
  24-hour soak would have needed about 300 GB. It is replaced by compressed
  per-entity blocks (`coverage-index-v2.sqlite`) with no per-row keys.
- **Growth with collection length.** Each window replayed Binance from its
  connection start (Binance is seeded only on connect) and re-decoded all
  operational and reference rows. One Binance replay now captures state at every
  window end, and those rows are decoded once per reader.
- **Read-only commands.** `verify` and `export-market` had started building and
  locking the index, so they failed while a quality worker ran. They now use a
  current index read-only, or read the archive directly.
- **Index projection identity and exit codes.** A changed field projection now
  rebuilds the index. A failed derived drain exits 5; 3 remains raw durability.

Tests: 540 passed (full suite), including new regressions for indexed/direct
equivalence across a restart and a Binance sequence gap, a single shared Binance
replay, read-only verify/export under a held index lock, and rebuild on a stale or
changed projection. Ruff passed. The 45-minute archive was not rescored in this pass;
its old v1 index can be deleted.

## Offline coverage correction — 1 October 2026

Quality policy v3 adds an incremental, transactional coverage index over immutable
normalized parts. Reconstruction selects the market's outcome books and Binance,
preserves older seeds/bootstrap evidence, and stops required feed coverage at expiry.
Epoch reports retain provenance without repeatedly copying full depth ladders.
Completed windows publish first; unchanged completed reports are reused. Progress
is visible separately from the last complete immutable publication.

Shutdown now performs a final synchronous normalization/quality drain after stopping
the background workers. `capture_clean` and `derived_processing_complete` are
separate receipt fields. A derived failure remains visible even when raw capture
finished cleanly. Status reads these receipts and current quality progress.

Validation: 66 warehouse/replay/integration tests passed together; two subsequent
publication regressions also passed in a focused five-test run. A further CLI
regression verifies a distinct exit code on a failed derived drain; index/publication checks
were rerun after narrowing and compressing the index projection. Ruff passed. The
full repository suite was deliberately not repeated. New cases cover incremental
reuse, instrument filtering, preserved older seeds, compaction rebuild equivalence,
expiry-bounded coverage, completed-window priority/cache reuse, new unnormalized
sealed inputs, and separate capture/drain success and failure states.

Research assessment: the data foundation is sufficient to move on to exploratory
strategy integration once selected recorded windows are checked. Do not expand
warehouse scope merely to pursue lower offline reporting latency. Strict, scored
profitability studies still need verified archive integrity, selected-window feed
coverage, attested settlement/threshold/payout semantics and historical fees. The
current replay accepts scripted intents; strategy adapters and queue-sensitive
maker/pair execution capabilities remain strategy-phase work. Data collection alone
does not implement those capabilities.

The historical live results below remain evidence from the earlier code. They do
not qualify the revised publisher's sustained live performance. Restart, soak and
final-code load measurement remain operator gates, not reasons to rewrite otherwise
working components.

Updated 1 October 2026. The reviewed coding corrections and offline features are implemented. **Production/live qualification remains pending operator validation.** A 5-minute connectivity capture and a 45-minute capture have been run. The 45-minute capture saved every message and contains five finished windows after warm-up. Its quality publication did not catch the end of the run. Restart, the 24-hour soak, and the final benchmark have not been run.

## Implemented offline

- Standalone read-only collector, strict effective profiles and credential-only dotenv loading.
- Durable raw capture, bounded intake/sealing backlog, acknowledgements after fsync, recovery and immutable manifests.
- Discovery, expected-window evidence, both outcome subscriptions, reference/Binance connectors and durable resolution obligations.
- Shared full readiness predicate for verification, exports and strict replay. Missing feeds, incomplete normalization, missing closure and unqualified payout mappings cannot pass.
- Exports retain older snapshot/bridge dependencies and original reception times. Later metadata cannot expand the decision horizon. Effective profiles, provider-policy evidence and archive verification are pinned in the bundle.
- Latest instrument mappings produce one payout per outcome despite repeated metadata observations. Conflicting terminal observations remain conflicting across later answers and process restarts.
- Unclean-session coverage ends at last confirmed evidence; earlier valid intervals survive reconnects.
- Streamed compaction with bounded parts, stable logical checksums and full reader ordering. Pinned old builds remain usable. Readers open nonoverlapping parts as needed.
- Supervised periodic quality publication, immutable coverage/boundary/book/settlement artifacts, cache reuse for completed markets and compact heartbeat status.
- Checksum-pinned synthetic provider fixtures, windowless/non-price extension evidence and offline scripted replay.

## Automated evidence

The correction regression suite covers readiness, repeated metadata, old export seeds, late metadata, crash intervals, persistent resolution conflicts, streamed compaction, immutable quality publication, fixture provenance, worker restart, real background-worker shutdown/publication without network, and lazy reader opening.

Final full run: **530 tests passed in 27.03 seconds**, including 62 warehouse/research tests (12 new completion regressions). Targeted Ruff checks passed for the warehouse, research, warehouse/replay tests and benchmark script. Offline configuration validation passed with reference credentials present; no values were printed and no provider was contacted.

## Five-minute connectivity capture

Root `var/warehouse_phase_b_quick`, collection `9fcb456e9a274587b645a30ac44b9da4`, session `2d77b56135094ef58d79edcf9cd27d7f`, clean stop after about 5.1 minutes. Shutdown reported `received = appended = fsynced = 178748`, an empty queue, and a healthy writer. Binance, Polymarket CLOB, and the PolyBolt reference feed were connected. Gamma, resolution HTTP, and the CLOB clock all returned data, so the earlier TLS hostname failure did not recur here. Discovery marked 16 windows `FOUND` and 275 later windows `DISCOVERY_UNCERTAIN`. Unknown messages, parse failures, drop counters, and recent gaps were empty. Semantic readiness stayed `UNSUPPORTED`.

This is evidence that the recorder can attach and keep every received message. It is too short for the 15-minute warm-up, so required-feed coverage stays `GAP`. The last quality publication (7 markets) finished before the final messages and is a mid-run snapshot.

## Deliberately pending operator gates

1. Provider access succeeded on the 45-minute capture with certificate checks left on. Gamma, CLOB, resolution, PolyBolt, and Binance all wrote data, and the CLOB clock was `OBSERVED`.
2. Obtain and attest the actual market rule, official threshold source/value availability, payout status/index/unit contract and historical fee/order constraints. Unknown facts remain unsupported.
3. The 45-minute capture contains five finished windows after warm-up. Finish `quality` and `verify` for that collection, then restart it. The on-disk quality snapshot is from about 17 minutes before shutdown.
4. Run a 24-hour soak crossing UTC midnight and follow selected pending resolutions/audits.
5. Measure final-code throughput, resource use and processing lag. The earlier 2.7-million-message benchmark predates subsequent changes and is not a final-code qualification.

Strategy adapters, model training, live quoting and actual pair merge/claim execution are outside Phase B. Replay payout scoring is retrospective and does not recycle settlement cash into earlier trades.

## Handoff

- [Operator validation commands](operator_validation.md)
- [Operating guide](../../latest/data_warehouse.md)
- [Provider contract and remaining unknowns](provider_contract_report.md)

The correct release description is **offline implementation complete; live qualification pending**, not production-qualified or profitable-strategy-ready.
