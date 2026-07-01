# Group D Implementation Summary (M8 WS-primary cutover)

**Implemented:** 2026-06-29  
**Scope:** WS-primary authoritative mode, readiness tracker production wiring, REST policy enforcement, reconnect/gap handling, rollback path.

**M8 status:** **`M8_VALIDATED`** on E4 `m8_validation_002c` (2026-06-30). See [m8_validation_report.md](m8_validation_report.md).

## Delivered

| Track | Modules | Tests |
|-------|---------|-------|
| **D1 — Readiness** | `market_data/readiness.py` (enhanced), `readiness_runtime.py`, facts `market_readiness_transition`, `market_data_health_block` | `test_market_readiness_states.py`, `test_ws_primary_readiness_runtime.py` |
| **D2 — WS-primary** | `ingestion/market_ws_ingest.py` (`primary_mode`), `runtime/app.py`, `runtime/config.py` (`MarketDataRestConfig`, `primary_enabled`) | `test_ws_primary_cutover.py` |
| **D3 — REST policy** | `market_data/decision_gate.py`, `paired_binary_run.py`, `monitor.py` | `test_rest_fallback_policy.py`, `test_cutover_acceptance_criteria.py` |
| **D4 — Reconnect/gap** | Gap on authoritative store, `note_reconnect_gap`, sequence handling | `test_ws_primary_reconnect_gap.py`, `test_ws_primary_event_ordering.py` |
| **D5 — Rollback** | Config-only rollback to shadow+REST+observe_only | `test_ws_primary_cutover.py`, `test_cutover_acceptance_criteria.py` |

## Config (WS-primary)

```yaml
runtime:
  market_data:
    websocket:
      primary_enabled: true
      shadow_enabled: false
    rest:
      poll_enabled: false
      bootstrap_on_startup: true
      recovery_on_reconnect: true
    quality:
      enforcement_mode: enforce
      require_ws_primary_for_entry: true
      allow_rest_recovery_for_exit: true
      allow_rest_recovery_for_entry: false
```

Scenario: `config/scenarios/live_paired_binary_ws_primary.yaml`

## Rollback (one config change)

```yaml
runtime:
  market_data:
    websocket:
      primary_enabled: false
      shadow_enabled: true
    rest:
      poll_enabled: true
    quality:
      enforcement_mode: observe_only
```

## Runtime posture

- **WS-primary mode:** WS writes `coord.market_state` with `source_quality=WS_PRIMARY`; coordinator wakes on authoritative updates only.
- **REST:** bootstrap/recovery only in WS-primary; poll disabled when `poll_enabled=false` and WS connected.
- **Entries / TP:** blocked unless `TRADING_ENABLED` + quality PASS + WS_PRIMARY.
- **Stop / urgent exit:** M3 degrade/emergency rules; REST_RECOVERY exit allowed when configured (reduce-only).
- **Strategy logic / params:** unchanged.

## Test results

52 M8 + paired-binary regression tests passed locally (pre–Group E). Post–Group E hardening: **26** M8-focused tests passed (duplicate-hash fix, analyzer, rollback drill).

## Group E validation (partial)

| Run | Run ID | Duration | Result |
|-----|--------|----------|--------|
| `m8_validation_002` | `c8081d7c-5ed9-4277-b759-b995cd7b4727` | ~161 s | Lifecycle DONE; latency_chain ack/fill null (fixed E3) |
| `m8_validation_002c` | `04056d08-b3b2-475b-a7cf-6cbb0138a3b0` | ~336 s | **M8_VALIDATED** — strict p95/p99 pass, activation deferred until fresh |

**E4 hardening:** `DecisionContext.ACTIVATION`; activation freshness gate; timeout `timeout_exit` snapshots; split analyzer metrics.

**Status:** **`M8_VALIDATED`**

## Remaining before M8 sign-off

See [m8_validation_report.md](m8_validation_report.md) and [m8_validation_plan.md](m8_validation_plan.md) criteria 1–12. Hard gates: ≥30 min or lifecycle, planner/latency on fills, disconnect/reconnect drill on fixed build, market meeting `min_usd` at `position_size: 5`.

## Not implemented (by design)

- M9 baseline rerun / before-after report
- Strategy threshold / TP / SL / survivor changes
