# N2 — Real read-only data adapters

**Status:** planned (no implementation in this planning commit)  
**Milestone folder:** `Docs/implementation/n2_real_data_adapters/`  
**Depends on:** N1 frozen source recommendations  
**Unblocks:** N3 (PTB/basis), N4 (real OBSERVE)

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
| Gamma discovery | Resolve BTC 5m market by slug; token/outcome map; start/end; rule metadata |
| RTDS Chainlink | `wss://ws-live-data.polymarket.com` · topic `crypto_prices_chainlink` · `btc/usd` |
| Direct Binance Spot WS | Primary trading reference \(S\) (extend existing `@trade` adapter) |
| Optional RTDS Binance | Comparison / fallback (`crypto_prices` / `btcusdt`) — not primary \(S\) unless N1 says otherwise |
| CLOB market WS | Books by discovered YES/NO token IDs |
| Time sync | SNTP (or equivalent) + optional Binance `/time` cross-check behind `TimeAuthority` |
| Lifecycle | Connect, subscribe, heartbeat, reconnect/backoff, resubscribe, dedupe, shutdown |
| Facts | Raw normalized ingress facts for audit/replay |

**REST:** used for discovery, clock cross-check, optional book snapshot recovery —
**outside** the hot decision path.

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
| Gamma discovery | `adapters/polymarket/discovery.py` | Strategy |
| CLOB market WS | `adapters/polymarket/ws_adapter.py` | Execution/OMS |
| RTDS client | `adapters/polymarket/rtds_*.py` (new) | Strategy / valuation |
| Binance WS | `adapters/binance/ws_adapter.py` | OMS |
| Normalize | `adapters/*/normalize.py` | Thresholds / Z-Gap policy |
| TimeAuthority live impl | `core/time_authority.py` + small sync helper | Strategy |
| Connection supervisor | `runtime/` or `adapters/` composition helper | Z-Gap formulas |
| Raw facts | Host / reporting sink | Recalculate FV |

---

## 8. Contracts, ports, and data structures to add or evolve

| Contract | Evolution |
|----------|-----------|
| `MarketDiscovery` | Keep; harden slug + fallback + rule packaging |
| `MarketDataAdapter` | Keep async `run`/`stop`; may fan multiple sockets |
| Settlement reference event | New normalized event (distinct from Binance `ReferencePriceUpdated`) |
| Reference store | May need dual series: trading vs settlement-associated |
| `TimeAuthority` | Add network-synced implementation; keep `FakeTimeAuthority` |
| Readiness / freshness | Explicit feed-ready flags per source |
| Sequence / dedupe keys | `(source, symbol, source_ts, value)` or provider sequence if present |

**Subscription identity:** CLOB books subscribe by **token IDs** validated against
the discovered market/window — never by guessing from a prior window.

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
src/tyrex_pm/core/time_authority.py
src/tyrex_pm/market_data/*                         # dual-ref / freshness if needed
src/tyrex_pm/runtime/live_runner.py                # lifecycle reuse (generic)
tests/                                             # adapter unit + recorded fixtures
```

Strategy / RiskEngine / planner / OMS / Portfolio: **unchanged** in N2.

---

## 10. End-to-end data or control flow

```text
Gamma resolve(window)
  → BinaryMarket {market_id, tokens, start, end, rule}

TimeAuthority.sync() → READY | DEGRADED | UNSYNCHRONIZED

Parallel feeds:
  RTDS Chainlink → SettlementRefUpdated(source_ts, receive_ts, value)
  Binance Spot   → ReferencePriceUpdated(...)
  [optional] RTDS Binance → comparison series
  CLOB market WS → BookUpdated(token_id, ...)

→ Stores (authoritative)
→ Readiness aggregate
→ (N3+) PTB capture / basis
→ (N4+) sealed ZGapDecisionSnapshot

Shutdown:
  stop Event → cancel tasks → close sockets → flush facts → exit
```

One-run vs continuous: same adapters; continuous orchestration only changes
**when** discovery/resubscribe runs (N4).

---

## 11. Failure and degraded-mode behavior

| Failure | Behavior |
|---------|----------|
| Connect failure | Backoff reconnect; mark feed not ready |
| Heartbeat timeout | Treat as disconnect; reconnect + resubscribe |
| Gap after reconnect | Invalidate affected book/ref freshness; optional REST snapshot rebuild for books |
| Duplicate message | Dedupe; do not double-update stores |
| Out-of-order `source_ts` | Policy: ignore older; fact `out_of_order` |
| Wrong token subscription | Fail closed; do not publish books under mismatched market_id |
| Time sync fail | `TimeAuthority` DEGRADED/UNSYNCHRONIZED; consumers block trading decisions later |
| RTDS down, Binance up | Trading ref may be fresh; settlement ref stale → basis/PTB not ready |

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
4. CLOB market WS subscribes only to token IDs from a validated `BinaryMarket`.
5. Heartbeat + reconnect + resubscribe behaviors covered by tests.
6. Network `TimeAuthority` can reach READY with measured `uncertainty_ms` in a manual net test; Fake remains for offline.
7. No OMS / user-order channel / mutation capability added.
8. Architecture import firewalls hold.
9. Full pytest suite green.

---

## 17. Expected deliverables

- RTDS + dual-reference + time-sync adapters
- Extended discovery readiness
- Recorded fixtures + unit tests
- Operator smoke notes for read-only connectivity
- N2 acceptance note under this folder

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
| Clock source set | SNTP primary + Binance cross-check (legacy-proven concept) |
| Multi-feed supervisor ownership | Prefer generic runtime helper, not Z-Gap module |

---

## 20. Expected commit boundary

```text
N2 commit theme:
  "Add read-only RTDS, dual-reference, and time-sync adapters"

Include: adapters, TimeAuthority live impl, market_data freshness, tests, N2 acceptance docs
Exclude: Z-Gap host real-input productization, PTB lock policy completion (N3), OMS, live orders
```
