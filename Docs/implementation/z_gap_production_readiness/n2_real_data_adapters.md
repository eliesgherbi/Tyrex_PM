# N2 — Real read-only data adapters

**Status:** `PASS_WITH_ENVIRONMENT_BLOCKER` (executed + correction)  
**Acceptance:** [n2_acceptance_report.md](n2_acceptance_report.md)  
**Document:** `Docs/implementation/z_gap_production_readiness/n2_real_data_adapters.md`  
**Depends on:** N1 frozen source recommendations  
**Unblocks:** N3 offline PTB/basis wiring; N3 live acceptance needs healthy Polymarket TLS  
**Note:** Offline adapters + timing/UpDown proofs complete. Polymarket live smoke on
this host is blocked by conclusive TLS hostname mismatch (`ENVIRONMENT_NETWORK_BLOCK`).
Binance Spot + ClockSyncSnapshot are live-OK.

---

## 1. Objective

Implement reusable, provider-specific **read-only** adapters that emit normalized
contracts for:

- Polymarket market discovery (Gamma);
- Polymarket RTDS Chainlink prices;
- Direct Binance Spot prices;
- Optional Polymarket RTDS Binance comparison/fallback;
- Polymarket CLOB market WebSocket books (token-ID subscriptions);
- Corrected time and source/receive timestamp handling.

N2 delivers trustworthy inputs. It does **not** enable Z-Gap OBSERVE/SHADOW
product runs on those inputs (N4/N5) and does **not** add OMS capability.

---

## 2. Why the milestone exists

Current stack already has Gamma discovery, CLOB market WS, and Binance trade WS
for momentum live observe/shadow. Z-Gap still lacks:

- RTDS Chainlink settlement-reference feed;
- Network-backed `TimeAuthority` sync;
- Unified async lifecycle (heartbeat, reconnect, resubscribe, dedupe) suitable
  for window rollover;
- Normalized settlement ticks feeding future PTB capture (N3).

Without N2, N4 cannot leave fixtures.

---

## 3. Scope

| Adapter / capability | Role |
|----------------------|------|
| Gamma discovery | Resolve BTC 5m market by slug; **Up/Down** token map by label; start/end; rule metadata |
| RTDS Chainlink | `wss://ws-live-data.polymarket.com` · topic `crypto_prices_chainlink` · `btc/usd` |
| Direct Binance Spot WS | Primary trading reference \(S\) (extend existing `@trade` adapter) |
| Optional RTDS Binance | Comparison / fallback (`crypto_prices` / `btcusdt`) — not primary \(S\) unless N1 says otherwise |
| CLOB market WS | Books by discovered **Up/Down** token IDs (label-mapped) |
| Clock sync provider | Adapter/ops emits `ClockSyncSnapshot` (SNTP, Binance `/time`, OS monitor) — **not** inside core |
| TimeAuthority (core) | Interprets sync evidence → corrected UTC, monotonic, uncertainty, readiness |
| Lifecycle | Connect, subscribe, heartbeat, reconnect/backoff, resubscribe, dedupe, shutdown |
| Ingress evidence | Append-only raw facts retaining late/out-of-order events |
| Prep support | APIs usable by N4 prepared-next discovery/subscribe without activating session |

**REST:** used for discovery, clock cross-check, optional book snapshot recovery —
**outside** the hot decision path.

**Outcome mapping:** `"Up"` → `UP`, `"Down"` → `DOWN` by normalized label only.
Never by array position. Reject missing/duplicate/unknown/ambiguous maps.

---

## 4. Explicit non-goals

- No OMS / order submission / user channel auth for trading
- No PTB lock policy productization (N3) beyond emitting ticks
- No Z-Gap host product wiring for real inputs (N4)
- No ShadowOMS or LiveOMS changes
- No Z-Gap-specific branches in RiskEngine / planner / OMS
- No imports from `old/` or `runtime/r7*`
- No HTML scraping as primary feed
- No continuous Z-Gap OBSERVE acceptance (N4)

---

## 5. Dependencies and entry criteria

| Criterion | Source |
|-----------|--------|
| N1 freeze on preferred sources | N1 acceptance |
| Existing ports | `adapters/protocols.py` (`MarketDiscovery`, `MarketDataAdapter`) |
| Existing adapters to extend | `discovery.py`, `ws_adapter.py` (CLOB), `binance/ws_adapter.py` |
| Domain types | `BinaryMarket`, book/reference events, future settlement-ref event |
| Official RTDS docs | Polymarket RTDS documentation |

---

## 6. Decisions that must already be frozen

- Ports/adapters own network I/O; strategy never calls venues
- Normalized events only enter stores → indicators → sealed snapshots
- Binance ≠ Chainlink truth
- One-run and continuous use the **same** adapters; orchestration differs later
- Atomic epochs remain mandatory (adapters must not mix timestamps silently)

From N1 (required before coding N2):

- Primary Chainlink path (expected: RTDS `crypto_prices_chainlink`)
- Primary trading \(S\) path (expected: direct Binance)
- Whether RTDS Binance is optional comparison only

---

## 7. Responsibility / module ownership

| Concern | Owner package | Must not |
|---------|---------------|----------|
| Ports | `adapters/protocols.py`, domain event types | Strategy imports |
| Gamma discovery | `adapters/polymarket/discovery.py` | Strategy; positional Up/Down mapping |
| CLOB market WS | `adapters/polymarket/ws_adapter.py` | Execution/OMS |
| RTDS client | `adapters/polymarket/rtds_*.py` (new) | Strategy / valuation |
| Binance WS | `adapters/binance/ws_adapter.py` | OMS |
| Normalize | `adapters/*/normalize.py` | Thresholds / Z-Gap policy |
| Clock sync **provider** | `adapters/` or ops module (SNTP, Binance `/time`, OS monitor) | Mutate system clock; live inside `strategies/` |
| `TimeAuthority` (core) | `core/time_authority.py` — interpret `ClockSyncSnapshot` only | Perform network I/O |
| Connection supervisor | `runtime/` or `adapters/` composition helper | Z-Gap formulas |
| Raw facts | Host / reporting sink | Recalculate FV; drop late ticks silently |

---

## 8. Contracts, ports, and data structures to add or evolve

| Contract | Evolution |
|----------|-----------|
| `MarketDiscovery` | Keep; harden slug + fallback; **label-based Up/Down** packaging |
| `MarketDataAdapter` | Keep async `run`/`stop`; may fan multiple sockets |
| Settlement reference event | New normalized event (distinct from Binance `ReferencePriceUpdated`) |
| Reference store | Dual series: trading vs settlement-associated |
| `ClockSyncSnapshot` | Normalized sync evidence from adapter/ops provider |
| `TimeAuthority` | Interprets snapshots → view; keep `FakeTimeAuthority` |
| Readiness / freshness | Explicit feed-ready flags per source |
| Sequence / dedupe keys | Prefer provider sequence IDs when available; else `(source, symbol, source_ts, value)` without erasing distinct meaningful events |
| Ingress buffer | Bounded event-time buffer for PTB lateness (budget frozen in N3 from N1) |

**Subscription identity:** CLOB books subscribe by **token IDs** from validated
Up/Down mapping for the market/window — never by guessing from a prior window.

---

## 9. Expected files / modules affected

```text
src/tyrex_pm/adapters/protocols.py
src/tyrex_pm/adapters/polymarket/discovery.py
src/tyrex_pm/adapters/polymarket/ws_adapter.py
src/tyrex_pm/adapters/polymarket/normalize.py
src/tyrex_pm/adapters/polymarket/rtds_*.py          # new
src/tyrex_pm/adapters/binance/ws_adapter.py
src/tyrex_pm/adapters/binance/normalize.py
src/tyrex_pm/adapters/*/clock_sync*.py             # network sync provider (new)
src/tyrex_pm/core/time_authority.py                # interpret only; no SNTP/HTTP
src/tyrex_pm/market_data/*                         # dual-ref / freshness / ingress buffer
src/tyrex_pm/runtime/live_runner.py                # lifecycle reuse (generic)
tests/                                             # adapter unit + recorded fixtures
```

Strategy / RiskEngine / planner / OMS / Portfolio: **unchanged** in N2.

**Production clock model:** deployment host clock disciplined by OS tooling where
possible; application **monitors and cross-checks** uncertainty via
`ClockSyncSnapshot`; application does **not** silently change the system clock.

---

## 10. End-to-end data or control flow

```text
Gamma resolve(window) with Up/Down label map
  → DiscoveredMarketBinding {outcomes UP/DOWN, book_legs, BinaryMarket yes←Up/no←Down slots}

ClockSyncProvider → ClockSyncSnapshot
TimeAuthority.view() → READY | DEGRADED | UNSYNCHRONIZED (+ offset, uncertainty, snapshot_id)

Parallel shared feeds (survive rollover):
  RTDS Chainlink → SettlementRefUpdated(
       ts_event=source_ts,
       ts_received=receive_wall_raw_utc,
       ingress={receive_wall_corrected_utc, receive_monotonic_ns, clock_*})
  Binance Spot   → ReferencePriceUpdated(... same raw/corrected contract ...)
  [optional] RTDS Binance → comparison series (never primary S)

Market-specific:
  CLOB market WS → BookUpdated(token_id from Up/Down label map; active and/or prepared-next)

→ Append-only ingress facts (retain late/OOO; store raw wall)
→ Current-price view may reject older updates after recording them
→ Stores / readiness
→ (N3+) PTB capture / causal basis
→ (N4+) sealed ZGapDecisionSnapshot on **active** session only

Shutdown:
  stop Event → cancel tasks → close sockets → flush facts → exit
```

One-run vs continuous: same adapters; continuous orchestration (N4) owns
prepared-next discover/subscribe and atomic promote.

---

## 11. Failure and degraded-mode behavior

Exposure-state matrix (initiative-wide; adapters contribute readiness signals):

| Exposure state | Failure behavior |
|----------------|------------------|
| FLAT | Block new exposure |
| ACTIVE with confirmed inventory | Continue risk management; seek safe exit |
| Inventory UNKNOWN | Reconcile; never guess quantity |
| Entry order ambiguous | N/A in N2 (no OMS); surface feed ambiguity only |
| Exit partially filled | N/A in N2 |
| Resolution committed | N/A in N2 |

| Failure | Behavior |
|---------|----------|
| Connect failure | Backoff reconnect; mark feed not ready |
| Heartbeat timeout | Treat as disconnect; reconnect + resubscribe |
| Gap after reconnect | Invalidate affected book/ref freshness; optional REST snapshot rebuild for books |
| Late / out-of-order tick | **Record** in append-only ingress; current-price view may reject older update; do not erase audit evidence |
| Duplicate message | Dedupe without erasing meaningful distinct provider events |
| Wrong token / Up-Down map | Reject market; do not publish books under mismatched identity |
| Time sync fail | `TimeAuthority` DEGRADED/UNSYNCHRONIZED; **FLAT** → block entry desires later; **ACTIVE** → exit capability remains a host/risk concern |
| RTDS down, Binance up | Trading ref may be fresh; settlement ref stale → entry not ready |

### Late / out-of-order ingress policy

1. Raw ingress facts are append-only and retain late/OOO events.  
2. Current-price view may reject an older update **after** recording it.  
3. PTB capture uses a bounded event-time buffer and explicit lateness policy
   (budget measured in N1, frozen in N3).  
4. Prefer provider sequence IDs when available.  
5. Any boundary tick arriving late remains available for audit and mismatch evidence.  
6. Deterministic replay preserves original arrival order.

---

## 12. Persistence and restart behavior

- Adapters are process-local; no durable feed cursor required for N2
- On restart: full resync (discovery + subscribe + warm)
- Optional: persist raw ingress JSONL for replay tests
- Do not pretend in-memory books survive crash

---

## 13. Facts, metrics, and reporting

| Fact family | Examples |
|-------------|----------|
| Feed lifecycle | connect, subscribe, heartbeat miss, reconnect, resubscribe, stop |
| Tick ingress | source, symbol, source_ts, receive_ts, seq/gap |
| Discovery | slug, market_id, tokens, start/end, rule hash |
| Clock | sync status, uncertainty_ms, sntp vs binance skew |
| Readiness | per-feed ready/stale |

Labels: raw ingress facts are **observed**, not estimated economics.

---

## 14. Configuration ownership and units

| Key (illustrative) | Owner | Units |
|--------------------|-------|-------|
| `gamma_base_url` | runtime/adapters config | URL |
| `rtds_url` | adapters | URL |
| `rtds_chainlink_symbol` | adapters | string (`btc/usd`) |
| `binance_symbol` | adapters | string (`BTCUSDT`) |
| `heartbeat_interval_s` | adapters (per channel) | s |
| `reconnect_backoff_s` | adapters | s |
| `book_snapshot_rest` | adapters | bool |
| `time.sntp_host` | TimeAuthority config | host |
| `time.max_uncertainty_ms` | mode policy (used N3+) | ms |

No Z-Gap θ thresholds in adapter config.

---

## 15. Test strategy

| Layer | Tests |
|-------|-------|
| Normalize | Golden payloads from recorded RTDS/CLOB/Binance samples |
| Dedupe / order | Out-of-order and duplicate fixtures |
| Reconnect | Fake WS: drop → reconnect → resubscribe invoked |
| Discovery | Mock Gamma; slug → tokens; reject incomplete |
| TimeAuthority | Fake network; READY vs DEGRADED |
| Architecture | Adapters do not import `strategies.z_gap`; strategy still cannot import adapters |
| Regression | Existing fixture observe/shadow still pass |

Use recorded fixtures; do not require live network in CI.

---

## 16. Deterministic acceptance criteria

1. RTDS Chainlink adapter receives and normalizes `btc/usd` ticks with source + receive timestamps.
2. Direct Binance Spot adapter remains the default trading reference path.
3. Optional RTDS Binance path exists behind config and is clearly labeled comparison/fallback.
4. CLOB market WS subscribes only to token IDs from a validated **Up/Down** `BinaryMarket`.
5. Heartbeat + reconnect + resubscribe behaviors covered by tests.
6. Clock sync provider emits `ClockSyncSnapshot`; core `TimeAuthority` interprets only (Fake remains for offline).
7. Late/OOO ticks are retained in ingress facts; current view rejection is tested.
8. No OMS / user-order channel / mutation capability added.
9. Architecture import firewalls hold.
10. Full pytest suite green.

---

## 17. Expected deliverables

- RTDS + dual-reference + time-sync adapters
- Extended discovery readiness
- Recorded fixtures + unit tests
- Operator smoke notes for read-only connectivity
- N2 acceptance note under this initiative

---

## 18. Stop conditions

- N1 leaves primary Chainlink path ambiguous and no provisional default is approved
- Official RTDS contract changes incompatibly without a redesign
- Implementing dual stores would require Z-Gap formulas in adapters
- Pressure to add LiveOMS “while we’re here”

---

## 19. Remaining risks and decisions

| Item | Notes |
|------|-------|
| RTDS Binance vs direct Binance | Prefer direct for \(S\); confirm with N1 latency |
| Book snapshot after gap | REST rebuild vs wait for WS snapshot |
| Clock sync provider | Adapter/ops; OS-disciplined host preferred; app monitors uncertainty |
| Multi-feed supervisor ownership | Prefer generic runtime helper, not Z-Gap module |
| Lateness budget | Measured N1; frozen N3 |

---

## 20. Expected commit boundary

```text
N2 commit theme:
  "Add read-only RTDS, dual-reference, and time-sync adapters"

Include: adapters, clock sync provider, TimeAuthority interpret path, market_data freshness, tests, N2 acceptance docs
Exclude: Z-Gap host real-input productization, PTB lock policy completion (N3), OMS, live orders
```
