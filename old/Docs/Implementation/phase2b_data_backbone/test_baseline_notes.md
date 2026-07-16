# Phase 2B Test Baseline Notes

**Purpose:** Document pytest baseline before M2B.0-B so new failures can be attributed correctly.  
**Recorded after:** M2B.0-A acceptance (2026-07-03)

---

## M2B.0-A acceptance tests (green)

```bash
pytest tests/test_market_event_serialization.py tests/test_market_event_sequencer.py -q
→ 13 passed
```

```bash
pytest tests/test_market_stream_ingest.py tests/test_ws_primary_event_ordering.py -q
→ 12 passed
```

---

## Full suite snapshot (pre M2B.0-B)

```bash
pytest -q
→ 954 passed, 11 failed, 1 skipped, 12 collection errors
```

### Collection errors (12)

`scripts.*` modules not on `PYTHONPATH` — import path issue in tests that import from `scripts/`:

- `test_analyze_pre_survivor_failure.py`
- `test_phase1_live_scenario_preflight.py`
- `test_phase1_order_policy_validator.py`
- `test_phase1_scenario_profiles.py`
- `test_phase1_simplified_profiles.py`
- `test_phase1_validator_enforcement_profiles.py`
- `test_replay_survival_advisory.py`
- `test_survival_parameter_config.py`
- `test_validate_activation_unwind_fail_classification.py`
- `test_validate_m8_ws_primary_run.py`
- `test_validate_phase1_classifications.py`
- `test_validate_phase2_force_flatten_classification.py`

### Failures (11) — reported pre-existing, unrelated to M2B.0-A

| Area | Symptom |
|------|---------|
| `pipeline.py` | `AttributeError: 'str' object has no attribute 'value'` in `_emit_group_b_observability` |
| `test_execution_planner.py` | 4 tests (planner observability path) |
| `test_validation_harness_runtime.py` | 2 tests (same pipeline issue) |
| `test_survival_stall_exit.py` | 3 tests (`result.stalled` assertion) |
| `test_allocation_clamp_grace_and_entry_reconcile.py` | 1 test (phase DONE vs BOTH_LEGS_ACTIVE) |
| `test_paired_binary_monitor_lifecycle.py` | 1 test (sellability gate) |

**Attribution:** None of these touch `core/events.py`, `ingestion/sequencer.py`, or M2B.0-A artifacts.

---

## M2B.0-B targeted regression gate

Run before and after M2B.0-B changes:

```bash
# Flag off (default)
pytest tests/test_market_stream_ingest.py \
  tests/test_market_ws_ingest.py \
  tests/test_ws_primary_event_ordering.py \
  tests/test_ws_primary_reconnect_gap.py \
  tests/test_market_state_store.py \
  tests/test_paired_binary_runtime.py -q

# Flag on
TYREX_EVENT_BACKBONE=1 pytest <same list> \
  tests/test_market_event_projection_equivalence.py -q
```

M2B.0-B acceptance does **not** require fixing full-suite pre-existing failures unless they block the targeted gate above.

---

## M2B.0-B targeted gate (after implementation)

**Before M2B.0-B code changes:** 41 passed (baseline gate).

**After M2B.0-B (flag off, default):**

```bash
pytest tests/test_market_stream_ingest.py tests/test_market_ws_ingest.py \
  tests/test_ws_primary_event_ordering.py tests/test_ws_primary_reconnect_gap.py \
  tests/test_market_state_store.py tests/test_paired_binary_runtime.py \
  tests/test_market_event_projection_equivalence.py \
  tests/test_market_event_serialization.py tests/test_market_event_sequencer.py -q
→ 59 passed
```

**After M2B.0-B (TYREX_EVENT_BACKBONE=1):** same command → 59 passed.

---

## M2B.0-C targeted gate (after implementation)

```bash
pytest tests/test_event_correlation_facts.py tests/test_latency_facts.py \
  tests/test_paired_binary_runtime.py tests/test_event_driven_survival_monitor.py \
  tests/test_paired_binary_phase2_regression.py -q
→ 33 passed

pytest tests/test_market_stream_ingest.py tests/test_market_ws_ingest.py \
  tests/test_ws_primary_event_ordering.py tests/test_ws_primary_reconnect_gap.py \
  tests/test_market_state_store.py tests/test_market_event_projection_equivalence.py \
  tests/test_market_event_serialization.py tests/test_market_event_sequencer.py -q
→ 47 passed
```

---

## M2B.0-C acceptance summary

```bash
pytest tests/test_event_correlation_facts.py tests/test_latency_facts.py \
  tests/test_paired_binary_runtime.py tests/test_event_driven_survival_monitor.py \
  tests/test_paired_binary_phase2_regression.py -q
→ 33 passed
```

**M2B.0-B regressions after M2B.0-C:**

```bash
pytest tests/test_market_stream_ingest.py tests/test_market_ws_ingest.py \
  tests/test_ws_primary_event_ordering.py tests/test_ws_primary_reconnect_gap.py \
  tests/test_market_state_store.py tests/test_market_event_projection_equivalence.py \
  tests/test_market_event_serialization.py tests/test_market_event_sequencer.py -q
→ 47 passed
```

**Full suite baseline remains:**

```bash
pytest -q → 954 passed, 11 failed, 1 skipped, 12 collection errors
```

---

## M2B.0-B acceptance summary

**Pre-change targeted M2B.0-B gate:**

```bash
pytest tests/test_market_stream_ingest.py tests/test_market_ws_ingest.py \
  tests/test_ws_primary_event_ordering.py tests/test_ws_primary_reconnect_gap.py \
  tests/test_market_state_store.py tests/test_paired_binary_runtime.py -q
→ 41 passed
```

**M2B.0-B flag-off gate:** → 59 passed  
**M2B.0-B flag-on gate (`TYREX_EVENT_BACKBONE=1`):** → 59 passed

**Full suite baseline remains:**

```bash
pytest -q → 954 passed, 11 failed, 1 skipped, 12 collection errors
```

---

## M2B.1-A targeted gate (after implementation)

```bash
pytest tests/test_event_sink_never_blocks.py tests/test_event_sink_segments.py \
  tests/test_event_sink_manifest.py tests/test_record_mode_import_isolation.py \
  tests/test_market_event_projection_equivalence.py tests/test_market_ws_ingest.py \
  tests/test_market_event_serialization.py tests/test_market_event_sequencer.py \
  tests/test_event_correlation_facts.py -q
→ 39 passed

python -m tyrex_pm.runtime.app record --help
→ OK
```

---

## M2B.1-A real ops smoke (accepted 2026-07-03)

```bash
python -m tyrex_pm.runtime.app record \
  --scenario config/scenarios/record_btc5m_single.yaml \
  --event-url "https://polymarket.com/fr/event/btc-updown-5m-1783090200"
```

Result:

```text
btc_5m_20260703_1455
43,115 JSONL MarketEvents
0 dropped events
book_delta: 41,217
book_snapshot: 14
ws_seq_gap: 1,884
payload.raw preserved
No trading artifacts
recording_ended_ts null in copied manifest; graceful shutdown follow-up opened.
```

---

## M2B.1-B targeted gate (after implementation)

```bash
pytest tests/test_market_discovery.py tests/test_event_sink_zstd.py \
  tests/test_record_heartbeat.py tests/test_event_sink_never_blocks.py \
  tests/test_event_sink_segments.py tests/test_event_sink_manifest.py \
  tests/test_record_mode_import_isolation.py tests/test_record_resolve_tokens.py \
  tests/test_market_event_projection_equivalence.py tests/test_market_ws_ingest.py \
  tests/test_market_event_serialization.py tests/test_market_event_sequencer.py \
  tests/test_event_correlation_facts.py -q
→ 68 passed

python -m tyrex_pm.runtime.app record --help
→ OK
```

Requires `pip install 'tyrex-pm[record]'` (or `zstandard>=0.22`) for zstd tests.

---

## M2B.1-B short discovery smoke (after coverage fix, 2026-07-03)

```text
- coverage_report.json produced correctly (coverage_pct: 100%)
- expired lookback market skipped (btc_5m_20260703_1850)
- pre-open lead respected (60s)
- no far-future premature recording
- manifests finalized on clean Ctrl+C
- zstd parse OK
- dropped_events = 0
- auto-stop at event_end_ts + post_close_grace_s verified (1855, 1900)
```

## 24h recorder validation

Status: running / pending user report

---

## M2B.2 targeted gate (external BTC feed)

```bash
pytest tests/test_binance_data_isolation.py tests/test_external_btc_events.py \
  tests/test_record_mode_import_isolation.py -q
→ 15 passed
```

## M2B.2 accepted with follow-up (2026-07-03)

```text
- PM + external BTC smoke succeeded
- external_btc_tick and clock_sync written
- external/btc_binance/ manifest finalized
- dropped_events = 0
- no trading imports
- follow-up: optional reconnect log noise reduction
```

## M2B.3 targeted gate (offline normalizer)

```bash
pytest tests/test_normalize_golden.py tests/test_research_import_isolation.py -q
→ 11 passed
```

## M2B.3-A RTDS fix gate (2026-07-05)

```bash
pytest tests/test_reference_price_events.py tests/test_polymarket_rtds_isolation.py \
  tests/test_market_channel_events.py tests/test_normalize_golden.py -q
→ 25 passed
```

```bash
pytest tests/test_market_discovery.py tests/test_event_sink_zstd.py tests/test_record_heartbeat.py \
  tests/test_record_mode_import_isolation.py tests/test_market_event_projection_equivalence.py \
  tests/test_market_ws_ingest.py tests/test_market_event_serialization.py \
  tests/test_market_event_sequencer.py tests/test_event_correlation_facts.py -q
→ 60 passed
```

Full suite (post RTDS fix):

```bash
pytest -q
→ 1091 passed, 13 failed, 1 skipped
```

Attribution: 13 failures match pre-existing survival/validation baseline (unchanged by RTDS fix).

Live smoke (12 min, `record_btc5m_rich.yaml`): `reference_price_event_count=770`, PTB `observed` for markets 1635/1640, parquet `reference_prices.parquet` 648 rows, `price_to_beat` observed rows present.

## M2B.4 planning gate (spec only — no implementation)

Status: spec rev 2 drafted; **awaiting user/reviewer sign-off**

Spec: [`spec_m2b_4_offline_lab.md`](spec_m2b_4_offline_lab.md)

Key additions (rev 2):

- Milestone boundary: M2B.4 proxy / M2B.5 replay / M2B.7 labeler
- Notebook 01 → `clean_markets.csv` precondition for 02–06
- `latency_prior.json` gate before Notebook 02 final candidates
- Notebook 02b resolution-tail analysis
- Per-bucket sample minimums + `insufficient` verdicts
- Multi-day loaders from day one
- Notebook 05 divergence census + liquidity-at-strike
- Notebook 03 `never_armed_and_lost` proxy (not M2B.7 label)
- Timestamp discipline; M2B.1-B blocks stable (non-provisional) claims only

No M2B.4 pytest baseline yet — implementation not started.

## M2B.4 implementation gate (2026-07-05)

```bash
pytest tests/test_research_lib_loaders.py tests/test_research_episode_formulas.py \
  tests/test_research_import_isolation.py -q
→ 9 passed
```

Smoke: `python research/m2b4/run_smoke.py` on `date=2026-07-05` → 26 markets included, artifacts in `research/output/m2b4/`.

## M2B.4-B exploratory layer gate (2026-07-06)

```bash
pytest tests/test_research_lib_loaders.py tests/test_research_episode_formulas.py \
  tests/test_research_import_isolation.py tests/test_research_exploratory_layer.py -q
→ 14 passed
```

Smoke includes `01`–`06_exploratory_trace.json/.md` under `research/output/m2b4/`.
