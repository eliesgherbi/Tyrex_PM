# N2 acceptance report — Real read-only data adapters

**Verdict:** `PASS_WITH_ENVIRONMENT_BLOCKER`  
**Date:** 2026-07-21  
**Branch:** `rest_project`  
**Scope:** reusable public-data adapters + contracts; no N3 PTB, no real OBSERVE/SHADOW, no OMS/live

---

## 0. Git / worktree (correction)

| Item | Value |
|------|--------|
| Branch | `rest_project` |
| N2 implementation commit | `9ed98bf7e99f7639049007656586e25c88788efe` — *Add read-only RTDS, dual-reference, and time-sync adapters* |
| Correction starting HEAD | `9ed98bf7e99f7639049007656586e25c88788efe` |
| Worktree before correction | Clean at N2 HEAD |
| Correction commit | *Close N2 connectivity and timing acceptance gaps* (tip of `rest_project` after correction) |
| Ending HEAD | Tip after correction — verify with `git rev-parse HEAD` / `git log -1` (reported in agent final response) |

**N2 commit files (exact):**

```text
Docs/implementation/z_gap_production_readiness/README.md
Docs/implementation/z_gap_production_readiness/n2_acceptance_report.md
Docs/implementation/z_gap_production_readiness/n2_real_data_adapters.md
src/tyrex_pm/adapters/binance/normalize.py
src/tyrex_pm/adapters/binance/ws_adapter.py
src/tyrex_pm/adapters/clock_sync.py
src/tyrex_pm/adapters/polymarket/discovery.py
src/tyrex_pm/adapters/polymarket/rtds_adapter.py
src/tyrex_pm/adapters/polymarket/rtds_normalize.py
src/tyrex_pm/adapters/polymarket/ws_adapter.py
src/tyrex_pm/adapters/protocols.py
src/tyrex_pm/core/events.py
src/tyrex_pm/core/ingress.py
src/tyrex_pm/core/snapshots.py
src/tyrex_pm/core/time_authority.py
src/tyrex_pm/domain/polymarket/discovery_binding.py
src/tyrex_pm/domain/polymarket/outcome_map.py
src/tyrex_pm/market_data/reference_store.py
src/tyrex_pm/market_data/settlement_store.py
src/tyrex_pm/runtime/feed_supervisor.py
tests/fixtures/n2/gamma_btc_5m_event.json
tests/fixtures/n2/rtds_binance_tick.json
tests/fixtures/n2/rtds_chainlink_tick.json
tests/test_n2_real_data_adapters.py
tools/n2_smoke/README.md
tools/n2_smoke/smoke_public_feeds.py
```

---

## 1. Verdict

**`PASS_WITH_ENVIRONMENT_BLOCKER`**

N2 adapters, timing contract, and Up/Down compatibility are fixture-proven.
Binance Spot + `ClockSyncSnapshot` were exercised live on this host. Polymarket
Gamma / RTDS / CLOB remain unreachable here due to a **conclusive environment
TLS interception** (not an N2 client configuration defect).

N3 offline implementation may start against normalized contracts. **N3 live
acceptance must run on a host with healthy Polymarket TLS.**

Do **not** treat this as a generic `PASS_WITH_BLOCKERS` without the environment
classification below.

---

## 2. Connectivity diagnosis (Task B)

**Classification:** `ENVIRONMENT_NETWORK_BLOCK`

**Evidence tool:** `python tools/n2_smoke/diagnose_connectivity.py`  
→ `var/reporting/n2/connectivity_diagnosis.json` (gitignored)

| Probe | Result |
|-------|--------|
| Proxy env vars present | None (`HTTP(S)_PROXY` / `ALL_PROXY` / `NO_PROXY` all false) |
| `api.binance.com` TLS | OK — CN/SAN `*.binance.com`, DigiCert |
| `stream.binance.com` TLS | OK |
| Binance HTTP `/api/v3/time` | 200 JSON |
| Binance trade WS | Connected |
| `gamma-api.polymarket.com` TLS | **FAIL** — `Hostname mismatch, certificate is not valid for 'gamma-api.polymarket.com'` |
| `ws-live-data.polymarket.com` TLS | **FAIL** — same hostname mismatch |
| `ws-subscriptions-clob.polymarket.com` TLS | **FAIL** — hostname mismatch (WS also timed out on handshake) |
| N1-style urllib Gamma | **Same TLS failure** as N2 Gamma client |
| Client stack difference material to TLS? | **No** — both use system trust + default websockets SSL |

**Ruled out:**

- `N2_CLIENT_DEFECT` — same failure on N1-style urllib and minimal SSL socket probe;
  Binance succeeds on the identical host/Python stack.
- TLS verification bypass — **not used** in product adapters; smoke
  `--insecure-ssl` **removed** in this correction.

**Not claimed:** transient provider outage (failures are cert SAN mismatch /
intercept, not intermittent 5xx).

---

## 3. Module / responsibility map

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
| Ingress provenance | `core/ingress.py` (`build_ingress_timing`) |
| Settlement / reference stores | `market_data/settlement_store.py`, `reference_store.py` |
| Multi-feed supervisor | `runtime/feed_supervisor.py` |
| Timing + Up/Down tests | `tests/test_n2_timing_and_updown.py` |
| Connectivity diagnosis | `tools/n2_smoke/diagnose_connectivity.py` |
| Manual smoke | `tools/n2_smoke/smoke_public_feeds.py` |

---

## 4. Timestamp field semantics (Task C)

Path:

`ClockSyncProvider` → `ClockSyncSnapshot` → `SnapshotTimeAuthority` →
adapter normalize (`build_ingress_timing`) → normalized event → ingress store.

| Field | Meaning |
|-------|---------|
| `source_ts` / `Event.ts_event` | Provider event time |
| `Event.ts_received` | **Raw** host wall UTC at ingress — **never** offset-corrected |
| `IngressMeta.receive_wall_raw_utc` | Same raw wall; must equal `ts_received` |
| `IngressMeta.receive_wall_corrected_utc` | `raw + clock_offset_ms` from `TimeAuthorityView` |
| `IngressMeta.receive_monotonic_ns` | Monotonic local ingress time |
| `IngressMeta.clock_offset_ms` | Applied offset (ms); `None` if unsynchronized |
| `IngressMeta.clock_uncertainty_ms` | From sync snapshot / view |
| `IngressMeta.clock_status` | `READY` / `DEGRADED` / `UNSYNCHRONIZED` |
| `IngressMeta.clock_snapshot_id` | Provenance id from `ClockSyncSnapshot.snapshot_id` |
| `IngressMeta.ingress_sequence` | Capture / ingress sequence |
| `IngressMeta.connection_generation` | Per-adapter reconnect generation |

**Invariant:** adapters must not write corrected time into `Event.ts_received`.
Normalize raises if `receive_wall_raw_utc != ts_received`.

**Without a time view:** corrected wall equals raw; status `UNSYNCHRONIZED`;
offset `None` — no silent “corrected” claim.

**Production uncertainty threshold:** not frozen in N2.

### Proof

1. Deterministic fake offset (±250 / −400 ms) in `tests/test_n2_timing_and_updown.py`
2. Normalized event carries expected corrected wall on `IngressMeta`
3. Raw remains on `ts_received` / `receive_wall_raw_utc` (auditable)
4. `DEGRADED` / `UNSYNCHRONIZED` propagate into `clock_status`
5. Hand-built meta with corrected stuffed into raw is rejected
6. Live Binance smoke reports **raw** and **corrected** `recv − source` separately
7. **Negative corrected latency** can remain when exchange `T` is ahead of
   corrected host wall (network path / exchange clock), or when a negative
   offset moves corrected wall earlier; N2 reports both latencies and does
   not force non-negativity

---

## 5. Up/Down compatibility boundary (Task D)

- Gamma outcomes mapped by **normalized labels only** (`map_up_down_outcomes`)
- `"Up"` → `UP`, `"Down"` → `DOWN`; array position never used
- Reversed provider order still maps correctly (fixture test)
- Public identity: `DiscoveredMarketBinding.outcome_semantics`,
  `up_token_id` / `down_token_id`, `book_legs`, `require_up_down_tokens()`
- `BinaryMarket.yes` / `.no` are **internal generic-binary slots only**
  (YES←Up, NO←Down) — documented on `compatibility_yes_no_note`
- No BTC discovery / book identity API treats outcomes as literal venue YES/NO

---

## 6. Connection lifecycle

Shared pattern across RTDS / Binance / CLOB:

connect → subscribe → PING/heartbeat → ready → reconnect+backoff → resubscribe → generation bump → stop

`FeedSupervisor` aggregates readiness (settlement stale + trading fresh → `DEGRADED`).

---

## 7. RTDS Binance behavior

- Documented `filters:"btcusdt"` supported  
- `auto` mode: filtered idle → unfiltered + local symbol filter  
- Role always `comparison_reference`; never primary \(S\)  
- Offline tests cover filtered/unfiltered and auto fallback  

---

## 8. Offline test evidence

```text
python -m pytest tests -q --tb=no
501 passed
```

Coverage: `tests/test_n2_real_data_adapters.py`,
`tests/test_n2_timing_and_updown.py`, fixtures under `tests/fixtures/n2/`.

---

## 9. Manual public-data smoke (Task E)

```text
python tools/n2_smoke/smoke_public_feeds.py --duration-s 28 --out var/reporting/n2/smoke_summary.json
```

TLS verification **always on** (no bypass flag).

| Feed | Connection | Messages / events | Timing | Status on this host |
|------|------------|-------------------|--------|---------------------|
| Gamma current/next BTC 5m | TLS fail | Fixture fallback for binding | n/a | Environment block |
| Up/Down label map | n/a | `outcome_semantics=UP_DOWN` on fixture binding | n/a | OK (fixture) |
| RTDS Chainlink | TLS fail | 0 live | Fixture normalize only | Environment block |
| Binance Spot | OK | 1079 ticks / 22s | raw lag ≈ 739 ms; corrected ≈ 402 ms; offset ≈ −338 ms; status READY | **Live OK** |
| RTDS Binance auto | TLS fail | 0 live (attempted filtered→unfiltered) | Fixture normalize only | Environment block |
| CLOB active books | TLS fail | 0 live | Fixture path only | Environment block |
| Prepared-next CLOB | API present; not live-published | — | — | Fixture/API only |
| ClockSyncSnapshot | OK | OS + Binance `/time`; uncertainty ≈ 125 ms | snapshot_id present | **Live OK** |
| Graceful shutdown | OK | — | — | OK |

**Paths remaining fixture-validated only until a healthy Polymarket host:**
Gamma live discovery, RTDS Chainlink, RTDS Binance (filtered/unfiltered),
CLOB books.

---

## 10. Decisions frozen

1. RTDS Chainlink = settlement-reference adapter path  
2. Direct Binance Spot = primary trading reference  
3. RTDS Binance = comparison/fallback only (`auto` filtered→unfiltered)  
4. Up/Down label mapping only; yes/no slots = compatibility layer  
5. `ClockSyncSnapshot` outside core; TimeAuthority interprets only  
6. Raw vs corrected walls are explicit and auditable  
7. Append-only ingress; current view may reject after record  
8. Prepared-next CLOB may subscribe without publishing as active  

---

## 11. Remaining OPEN (N3+)

- Exact Chainlink boundary sampling semantics  
- Crypto PTB HTTP / attestation production source  
- Max skew, lateness budget, PTB mismatch tolerance  
- Basis freshness/drift limits  
- Scope A timing numerics  
- Production clock uncertainty threshold  

---

## 12. Exact N3 readiness

| Allowed now | Blocked until healthy Polymarket host |
|-------------|----------------------------------------|
| Offline PTB/basis wiring against normalized events | Live RTDS/CLOB/Gamma acceptance samples |
| Use Binance + clock timing contract | Claim PROVEN live Polymarket latency |
| Fixture-backed adapter behavior | Freeze production uncertainty from this host alone |

N3 **must not** claim PROVEN boundary rule or freeze numeric thresholds without
clock-sync-backed Polymarket latency on a healthy TLS path.

---

## 13. Explicit exclusions confirmed

No PTB selection/lock, no basis math, no Z-Gap real-input host, no ShadowOMS/LiveOMS,
no auth/user channel, no orders, no TLS verify bypass, no `old/` imports, no N3
implementation started.

---

## 14. Test and git (final)

- Offline: **501 passed** (`python -m pytest tests -q --tb=no`)
- Correction commit: `Close N2 connectivity and timing acceptance gaps`
- Do not push; do not start N3 in this milestone
