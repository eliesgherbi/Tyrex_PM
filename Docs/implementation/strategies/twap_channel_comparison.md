# Compare the research and live TWAP channels

From the repository root in PowerShell:

```powershell
.venv\Scripts\python.exe scripts\compare_twap_channels.py --seconds 1200
```

This records both channels for 20 minutes (approximately four five-minute boundaries).
Use `--seconds 60` for a short connectivity check, or `--seconds 3600` for an hour.
Ctrl+C stops capture and writes the comparison from the evidence collected so far.
Each capture has a new directory under `var/observation/twap_channel_compare/`.
An explicit `--out` directory must be empty, so evidence is never mixed.

The script uses:

- The actual warehouse `record_stream` PolyBolt connector and its production normalizer.
- The actual live `RtdsChainlinkAdapter` and normalizers, with the live runner's
  25-second heartbeat setting. A small socket wrapper timestamps/saves inbound price
  frames before the adapter normalizes them.
- The existing credential loader for the PolyBolt API triplet in `.env` (or environment),
  with the same `TYREX_DATA_PM` / legacy `POLYMARKET` names as the collector. It does
  not initialize a trading client or load a wallet key into an execution component.
  Outbound authentication and its acknowledgement are not archived.

No collector is restarted and no warehouse, experiment or settlement state is changed.
This test records reference feeds only: it does not submit orders or run the strategy.
Keep TLS verification enabled; connection failures are evidence, not grounds to bypass it.

## Results

Open the `report.md` path printed at the end. Detailed results:

- `summary.json`: exact matching, maximum price difference, missing seconds,
  duplicates/conflicts, out-of-order observations, full-accuracy field counts,
  connection/error counts, five-minute boundary observations, and latency statistics.
- `pairs.csv`: one row for each exact common **live** source timestamp, both values,
  their exact Decimal difference and which channel arrived first.
- `ticks.jsonl`: normalized observations with original receipt/source times,
  monotonic arrival time, live/history classification and connection identity.
- `raw_polybolt.jsonl`, `raw_rtds.jsonl`: inbound price frames for inspecting
  precision, formatting and normalization. Non-price server/error/auth messages
  are not saved; bounded connection event identifiers are recorded separately.
- `events.jsonl`, `run.jsonl`: connection, sequence-gap and error events, capture
  settings and wall-clock versus monotonic-clock elapsed drift.

**Relative arrival:** a positive `rtds_minus_polybolt_arrival_ms` means RTDS arrived
later. Both arrivals use the same host's monotonic clock, so a fixed clock offset
does not affect this difference. Source-to-arrival absolute delay also includes
publication/aggregation delay and host-clock error; it is not pure network latency.

**History:** history ticks are retained but excluded from live latency and missing-live
counts. A later history snapshot cannot make a missed live update appear timely.
Boundary checks report the 5-second live / 30-second history bounds using the host
clock, without claiming a synchronized production time-authority measurement.

**Coverage:** missing seconds are computed at an explicitly assumed one-second
cadence over the overlap of the two live source ranges. Startup/shutdown tails are
excluded. This cadence is a diagnostic assumption, not a provider guarantee.
Matching values do not establish identical delivery or future reliability.

Verdicts:

- `MATCHED_ON_OBSERVED_INTERVAL`: at least ten common live timestamps, exact values
  agree, no conflicting duplicates, and no expected overlap seconds are missing.
  Review the measured latency and connection history too; this is not live qualification.
- `DIFFERENCES_OBSERVED`: exact-value conflicts or coverage gaps were observed.
  Tiny precision differences and genuine series differences must be interpreted separately.
- `MATCHED_VALUES_WITH_SHARED_GAPS`: the channels agree on their live timestamps
  and values, but both omit one or more expected seconds. This is a common cadence
  gap, not evidence that one transport lost an update the other received.
- `INSUFFICIENT_DATA`: fewer than ten common live timestamps. A channel unavailable
  due to authentication, DNS or TLS must never be described as an equality pass.

Complete matching exits with code 0; gaps, differences or insufficient evidence use code 2. This
allows simple scripted checks without mistaking an empty capture for success.

To regenerate a report without connecting again:

```powershell
.venv\Scripts\python.exe scripts\compare_twap_channels.py --analyze PATH_TO_CAPTURE
```

This preserves the raw recording and regenerates only the derived report/CSV/summary.
Keep the capture directory for a subsequent research/live timing review. No nearest
timestamp matching, interpolation or early publication of later-received history is used.
