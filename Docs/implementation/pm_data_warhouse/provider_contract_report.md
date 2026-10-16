# Provider contract report

Updated: 1 October 2026 (offline correction pass). Source-contract label stored on each collection: `2026-09-30-unqualified-live`.

This report records what the warehouse code is allowed to assume. It is a documentation and implementation review. It is not a live qualification of Polymarket, PolyBolt, or Binance.

## Evidence used

Public documentation already cited by the Phase B plan:

- Polymarket market channel: book and market-event messages, reconstructed from that protocol.
- PolyBolt: authenticated CLOB-credential subscription and a Chainlink BTC 60-second TWAP channel.
- Resolution HTTP: condition-level state, one selector family, at most 20 condition ids per request.
- Binance spot diff depth: buffer, REST bootstrap, update-id bridge, and a new epoch after a gap. The 5,000-level snapshot is labeled `bootstrap_5000_plus_observed_updates` and does not claim deeper levels.

Pinned optional dependencies for the warehouse extra: `httpx==0.28.1`, `pyarrow==23.0.1`, `duckdb==1.5.6`, `zstandard==0.25.0`. The trading package keeps `polymarket-client==0.5.0`. Warehouse modules do not import execution, runtime, or strategies.

A read-only GET of `https://gamma-api.polymarket.com/markets` from this workstation failed TLS hostname verification. No response body was accepted, and certificate checks were not disabled. No official threshold, payout unit, or terminal status was taken from a live payload.

## Supported by the current code

These behaviors are implemented and covered by offline tests. They describe the recorder, not a qualified research dataset.

- Raw JSONL capture with received, appended, and fsynced watermarks. Only fsynced bytes are durable. Queue overflow and the disk floor raise `DurabilityError` (process exit 3).
- Segment seal and zstd compression run on a separate sealer thread. The fsync thread closes the file and queues the path.
- Catalog SQLite is a rebuildable projection, including complete lines in an unsealed `.open.jsonl` tail.
- BTC five-minute slug selection lives in the BTC family. A windowless `event_driven_question` family and a non-price external payload use the same writer, normalizer, and reader. The live profile validator still accepts only the BTC five-minute profile.
- CLOB websocket snapshots seed a book epoch. REST book audits are stored and are not spliced into that epoch. CLOB continuity is `UNKNOWN_CONTINUITY`.
- Reference coverage uses the PolyBolt connection span. A five-second observation warning is staleness, and it is not a coverage gap. Without connection events, ticks stay point observations.
- Discovery expected windows come from the BTC family. A failed or empty probe does not downgrade a window already marked `FOUND`. A failed scan is `REQUEST_FAILED_NOT_PROOF_OF_ABSENCE`. An empty listing of an unseen slug is `NOT_YET_LISTED`. Capacity exclusion is `CAPACITY_EXCLUDED`.
- Normalization publishes versioned Parquet. Compaction stores logical checksums. Two exports of the same raw input and software version match on `quality.json`, `boundaries.json`, and derived logical rows.
- Decimal values stay exact. The DuckDB facade projects decimals as strings and timestamps as epoch-nanosecond integers.
- PolyBolt authentication frames are not archived. Sequence skips and drop counters are visible events.

## Unsupported until a policy file attests them

Semantic readiness is `UNSUPPORTED` with reason `official_threshold_and_payoff_contract_not_attested` unless `collections/<id>/provider_policy.json` says otherwise.

`semantic_status` returns `READY` only when all of these are true:

- `rule_status` is `SUPPORTED`
- `official_threshold` is `ATTESTED`
- the market rule fingerprint is listed in `supported_rule_fingerprints`

`SUPPORTED` requires `ATTESTED`, and `ATTESTED` requires `SUPPORTED`. The collection heartbeat calls this check with no fingerprint, so the heartbeat itself stays `UNSUPPORTED`.

Required policy keys: `schema_version` (1), `policy_id`, `evidence`, `terminal_statuses`, `payout_unit`. The only accepted payout unit is `USD_per_share`. Optional `payout_index_basis` accepts only `outcome_index`. Scoring labels are emitted for a `PROVIDER_TERMINAL` assessment only when that basis is set and the latest mapping has one unique instrument per contiguous outcome index. Repeated metadata observations do not multiply instruments.

The BTC family does not invent an opening threshold, a fee formula, payout units, or chain finality. Replay scores a qualified label after the decision loop. That credit does not increase cash available to earlier orders.

## Unresolved

These items stay unresolved until a recorded live session and a written attestation:

1. The BTC five-minute rule text, averaging and equality semantics, and which rule fingerprints are in force.
2. Whether an official threshold is exposed as structured data, and the time that value was observed.
3. Whether the reference feed includes the constituent prices behind a published TWAP.
4. Fee rate, exponent, rounding, tick size, and minimum size as they change over time. Scenario fees in replay are labeled `SCENARIO_ASSUMPTION_NOT_VERIFIED_HISTORICAL_FEES`.
5. Live CLOB snapshot continuity, reference acknowledgement behavior, decimal precision, and the meaning of provider timestamps. Chain id `137` and millisecond source times in the normalizer are code assumptions.
6. The provider's terminal status strings and payout vector layout. A fixture status such as `TEST_TERMINAL` is synthetic.
7. TLS, rate limits, and capacity on the recording machine. The failed certificate check above is an unpassed access gate for this workstation.

## Operator gates still open

Offline tests cover capture, normalization, export stability, discovery failure handling, and scripted payout scoring. They do not replace:

- a read-only live smoke of at least three complete windows after warm-up, including a restart
- a 24-hour soak that includes UTC midnight, with resolution follow-up resumed afterward
- saved measurements from `python scripts/benchmark_warehouse.py`

No sample export from a live session is pinned in this report.


## Offline completion corrections — 1 October 2026

The six reviewed defects have regression coverage: incomplete-data readiness, missing export seeds, duplicate instrument histories, crash coverage bounds, forgotten live resolution contradictions, and collection-sized compaction allocation. Archive verification and strict replay now share quality policy v2. Book reconstruction is version 2; source parsing and raw identities remain version 1. The added derived coverage tables are additive schema fields/datasets; existing raw/normalized builds are not overwritten.

A policy flag still does not manufacture a numerical opening threshold. Boundary candidates stay separate from official attestations. The operator must supply actual evidence for supported semantic and payout claims. The default policy remains unsupported.

The source fixture manifest is now [tests/fixtures/data_warehouse/manifest.json](../../../tests/fixtures/data_warehouse/manifest.json). Every example is labeled synthetic and checksum-pinned. No new live payloads were obtained during this correction pass. Earlier Binance recording and load artifacts exist under ignored `var/`; they do not qualify the revised implementation or establish Polymarket access.

Periodic quality publication runs in its own supervised process. It publishes stable reports and derived coverage with input-build lineage. Explicit verification/exports perform integrity checks; periodic reports alone do not assert verified integrity. Completed market reports can reuse immutable artifacts, preserving their assessment timestamp.

See [operator_validation.md](operator_validation.md) for the live smoke, restart, selected-market checks, 24-hour soak and synthetic throughput commands. The operator will run these gates separately.
