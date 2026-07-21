# N2 acceptance report — Real read-only data adapters

**Verdict:** `PASS_WITH_BLOCKERS`  
**Date:** 2026-07-21  
**Branch:** `rest_project`  
**Baseline HEAD (start):** `9f1e8a97b0c7f59997a2ae41e40a11435a1bd42b`  
**Scope:** reusable public-data adapters + contracts; no N3 PTB, no real OBSERVE/SHADOW, no OMS/live

---

## 1. Verdict

**`PASS_WITH_BLOCKERS`**

N2 implementation and offline tests are complete. N3 may proceed against the
normalized contracts, with OPEN numeric thresholds still unfrozen.

**Blocker (environment, not code):** On this host, Polymarket HTTPS/WSS endpoints
are intercepted (`CERTIFICATE_VERIFY_FAILED` / WS upgrade returns HTTP 200 HTML).
Manual smoke therefore validated Binance Spot + `ClockSyncSnapshot` live, and
relied on offline fixtures + N1 evidence for RTDS/CLOB/Gamma paths.

---

## 2. Module / responsibility map

| Concern | Module |
|---------|--------|
| Up/Down label map | `domain/polymarket/outcome_map.py` |
| Discovery binding (active / prepared-next) | `domain/polymarket/discovery_binding.py` + `adapters/polymarket/discovery.py` |
| RTDS normalize | `adapters/polymarket/rtds_normalize.py` |
| RTDS Chainlink / Binance adapters | `adapters/polymarket/rtds_adapter.py` |
| CLOB market WS | `adapters/polymarket/ws_adapter.py` |
| Binance Spot WS | `adapters/binance/ws_adapter.py` + `normalize.py` |
| Clock sync provider | `adapters/clock_sync.py` |
| TimeAuthority interpret | `core/time_authority.py` (`ClockSyncSnapshot`, `SnapshotTimeAuthority`) |
| Ingress provenance | `core/ingress.py` |
| Settlement store | `market_data/settlement_store.py` |
| Trading/comparison store | `market_data/reference_store.py` |
| Multi-feed supervisor | `runtime/feed_supervisor.py` |
| Offline tests | `tests/test_n2_real_data_adapters.py` |
| Manual smoke | `tools/n2_smoke/smoke_public_feeds.py` |

---

## 3. Normalized contracts

- `SettlementReferenceUpdated` + `SettlementReferenceSnapshot` (Chainlink / RTDS)
- `ReferencePriceUpdated` + `ReferencePriceSnapshot` (Binance Spot; RTDS Binance comparison)
- `IngressMeta` on ticks: wall receive, monotonic, uncertainty, sequence, connection generation, provider seq, fingerprint, late/OOO, role
- `ClockSyncSnapshot` + `ClockSourceObservation`
- `DiscoveredMarketBinding` with label-mapped `OutcomeMap` and rule fingerprint
- Feed roles: `settlement_reference` | `trading_reference` | `comparison_reference` | `market_book`

Binance is never labelled settlement truth.

---

## 4. Discovery behavior

- Deterministic slug `btc-updown-5m-{epoch}` + exact Gamma lookup
- `"Up"→UP`, `"Down"→DOWN` by label only (rejects positional, missing, duplicate, unknown, token mismatches)
- Validates slug vs `eventStartTime`, end = start+300s, Chainlink resolution source
- `prepare_next_btc_5m` / `session_role=PREPARED_NEXT` for N4 without activation
- BinaryMarket still exposes Up/Down via yes/no instrument slots (YES←Up, NO←Down) for compatibility

---

## 5. Connection lifecycle

Shared pattern across RTDS / Binance / CLOB:

connect → subscribe → PING/heartbeat → ready → reconnect+backoff → resubscribe → generation bump → stop

`FeedSupervisor` runs parallel adapters and aggregates readiness (settlement stale + trading fresh → `DEGRADED`).

---

## 6. Timing semantics

- `source_ts` = provider timestamp
- `ts_received` / `receive_wall_utc` = local wall UTC
- `receive_monotonic_ns` = monotonic
- `clock_uncertainty_ms` from TimeAuthority snapshot when wired (nullable until sync)
- Core `SnapshotTimeAuthority` applies offset; **no network I/O in core**
- Observed smoke (Binance `/time` cross-check): offset ≈ −400 ms, uncertainty ≈ 120–130 ms, disagreement ≈ 400 ms (not frozen)

---

## 7. Raw-ingress and ordering policy

1. Append-only `IngressRecord` in settlement/reference stores  
2. Record before rejecting stale `source_ts` from current view  
3. Late/OOO marked on normalize when provider time goes backwards  
4. No dedupe of distinct events solely because `(ts, value)` match  
5. Binance prefers trade id as provider sequence  
6. Lateness budget remains OPEN for N3  

---

## 8. RTDS Binance behavior

- Documented `filters:"btcusdt"` supported  
- `auto` mode: filtered idle → explicit fallback to unfiltered + local symbol filter  
- Role always `comparison_reference`; never primary \(S\)  
- Failover visible in health facts (`subscription_mode_fallback`)  

Offline tests cover filtered/unfiltered subscribe shapes and auto fallback.

---

## 9. Clock observations (smoke)

| Field | Value (approx) |
|-------|----------------|
| Primary | `os_clock` |
| Cross-check | `binance_api_time` |
| Estimated offset | ≈ −400 ms |
| Uncertainty | ≈ 120–130 ms |
| Disagreement | ≈ 400 ms |
| Status | READY (max_uncertainty gate relaxed for smoke apply) |

Negative Binance recv−source still possible until hosts consume corrected UTC; N2 exposes the snapshot for N3/N4.

---

## 10. Offline test evidence

```text
python -m pytest tests -q --tb=no
493 passed
```

New coverage in `tests/test_n2_real_data_adapters.py` (normalize, discovery rejects, stores, fake-WS lifecycle, clock, architecture imports). Fixtures under `tests/fixtures/n2/`.

---

## 11. Manual public-data smoke

Command:

```text
python tools/n2_smoke/smoke_public_feeds.py --duration-s 28 --out var/reporting/n2/smoke_summary.json
```

Result on this host (`pass_partial`):

| Check | Result |
|-------|--------|
| Gamma live | Blocked (TLS hostname mismatch / HTML intercept) |
| RTDS Chainlink | Blocked (WS upgrade → HTTP 200) |
| RTDS Binance | Blocked (same) |
| CLOB books | Blocked (same) |
| Binance Spot | **OK** (hundreds–thousands of trades) |
| ClockSyncSnapshot | **OK** (OS + Binance time) |
| Auth / orders | Not touched |

N1 capture previously proved RTDS Chainlink + Gamma on a working network path; offline fixtures encode those payloads.

---

## 12. Decisions frozen

1. RTDS Chainlink = settlement-reference adapter path  
2. Direct Binance Spot = primary trading reference  
3. RTDS Binance = comparison/fallback only (`auto` filtered→unfiltered)  
4. Up/Down label mapping only  
5. `ClockSyncSnapshot` outside core; TimeAuthority interprets only  
6. Append-only ingress; current view may reject after record  
7. Prepared-next CLOB may subscribe without publishing as active  

---

## 13. Remaining OPEN (N3+)

- Exact Chainlink boundary sampling semantics  
- Crypto PTB HTTP / attestation production source  
- Max skew, lateness budget, PTB mismatch tolerance  
- Basis freshness/drift limits  
- Scope A timing numerics  
- Production clock uncertainty threshold  

---

## 14. Exact N3 blockers

N3 must not claim PROVEN boundary rule or freeze numeric thresholds without:

1. Clock-sync-backed latency sample on a host with healthy Polymarket TLS  
2. Boundary windows distinguishing `first_ge` vs `last_le` when no exact-on-boundary tick  
3. Attestation source decision (SSR vs future API) kept out of hot path  

N3 **can** start wiring PTB capture against `SettlementReferenceUpdated` + ingress buffer hooks.

---

## 15. Explicit exclusions confirmed

No PTB selection/lock, no basis math, no Z-Gap real-input host, no ShadowOMS/LiveOMS, no auth/user channel, no orders, no `old/` imports.

---

## 16. Test and git

- Offline: **493 passed**  
- Commit theme: `Add read-only RTDS, dual-reference, and time-sync adapters`  
- Do not push; do not start N3 in this milestone  
