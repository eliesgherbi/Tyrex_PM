# 08 — Validation report

## R1

| Check | Result |
|-------|--------|
| Checkpoint commit | `630bac2acf67961a30b4be014d1df0434af967f1` |
| Message | `reset project with isolated legacy tree and minimal skeleton` |
| `.env` / `var/` in commit | Absent |
| Tests at R1 | 10 passed |

## R2

| Check | Result |
|-------|--------|
| Checkpoint commit | `ccccc969bb4877ae97e6e56c656b839739034425` |
| Message | `add deterministic event-driven core contracts` |
| Packages | `tyrex_pm.core`, `tyrex_pm.engine` |
| Pytest at R2 gate | **31 passed** |
| NautilusTrader | Absent |
| Intents / OMS / adapters | Not implemented (by design) |

### Rejected alternatives (R2)

- Delta book events in R2 (moved to R3 Option B after protocol evidence).
- Intent type hierarchy (R4).
- Venue tick rounding in core.

---

## R3 completion report

**R2 checkpoint hash (required):** `ccccc969bb4877ae97e6e56c656b839739034425`  
**Pytest after R3:** **64 passed** (no live network in default suite)  
**`.env` SHA256 (unchanged):** `27210C97AE37101DE48570130BBB517E572F3EB75160B5FC4C05CF178B91F772`  
**`var/`:** gitignored; live facts under `var/reporting/r3/` untracked  
**`old/` imports:** none  
**NautilusTrader:** none  

### Polymarket protocol evidence

| Source | Finding |
|--------|---------|
| Official CLOB market WS docs | `book` full snapshot; `price_change` deltas (`size` `"0"` removes); `tick_size_change` |
| Endpoint | `wss://ws-subscriptions-clob.polymarket.com/ws/market` |
| Subscribe body | `{"assets_ids":[...],"type":"market"}` |
| Live probe (2026-07-16) | Confirmed `book` + `price_change` payloads; near-expiry books can be one-sided |
| `old/` adapter | Stub only — not used |

**Design choice: Option B** — typed snapshot/delta/tick ingress; `MarketStateStore` owns reconstruction; `BookUpdated` is the store-emitted complete view.

### Binance stream

| Item | Value |
|------|-------|
| Stream | `wss://stream.binance.com:9443/ws/btcusdt@trade` |
| Data | Individual public trades |
| Event timestamp | Trade time `T` (ms) |
| Why sufficient | Dense prints for short-horizon \(P_t/P_{t-L}-1\) without auth |
| Reconnect | Backoff reconnect; momentum `reset()` on re-connect after first session |

---

### R3A — Deterministic fixture-driven vertical slice

#### Files / packages created

```text
src/tyrex_pm/core/book_events.py
src/tyrex_pm/domain/polymarket/
src/tyrex_pm/adapters/{protocols,polymarket/*,binance/*}
src/tyrex_pm/market_data/{registry,book_store,reference_store,freshness,executable,decision_snapshot}
src/tyrex_pm/indicators/momentum.py
src/tyrex_pm/signals/directional.py
src/tyrex_pm/strategies/framework_validation/reference_momentum.py
src/tyrex_pm/reporting/{jsonl,serialize,facts}
src/tyrex_pm/runtime/{config,observe_host,live_observe}
src/tyrex_pm/operations/
config/observe_fixture_r3.json
tests/fixtures/r3_observe_*.json
tests/test_r3_*.py
```

#### State ownership

| Owner | Responsibility |
|-------|----------------|
| InstrumentRegistry | Resolved BinaryMarket + YES/NO |
| MarketStateStore | Books, init/recovery, tick size |
| ReferenceDataStore | Latest Binance observation |
| ShortHorizonMomentum | Rolling buffer |
| Freshness | Derived at decision time only |

#### Freshness semantics

`FreshnessAssessment(is_fresh, age_ms, threshold_ms, timestamp_basis, reason_code)`  
Reasons: `FRESH` | `UNINITIALIZED` | `STALE` | `FUTURE_TIMESTAMP` | `MISSING_TIMESTAMP`  
Clock-injected; future beyond tolerance rejected; never latched permanently fresh.

#### Indicator / signal / decision

- Momentum: \(m_t = P_t / P_{t-L} - 1\); baseline = newest sample with `ts ≤ t−L`; no interpolation.
- Signal: `UP|DOWN|FLAT|UNAVAILABLE` with evidence (momentum, threshold, spreads, freshness, reason).
- Decision: `WOULD_ENTER_UP|WOULD_ENTER_DOWN|HOLD|SKIP` — no execution intent.

#### Fact schema

`FactEnvelope` schema_version=1 JSONL: runtime/market/adapter/book/reference/freshness/indicator/signal/observe_decision/failure.

#### Fixture sources

`tests/fixtures/r3_observe_complete.json` (UP path + delta), `r3_observe_down.json` (DOWN).

#### Tests and results

Covered: complete chain, determinism, UP/DOWN/FLAT/UNAVAILABLE, uninitialized/stale/future, empty/one-sided/VWAP, momentum history/OOO, fact round-trip, normalize, store snapshot/delta/tick/reconnect invalidate, config validation, architecture firewall.  
**Result: 64 passed** (default suite; no network).

---

### R3B — Public live read-only validation

| Item | Evidence |
|------|----------|
| Public endpoints | Gamma `https://gamma-api.polymarket.com/events`; PM market WS; Binance `@trade` |
| Private/trading ops | None (no wallet, keys, order endpoints); facts scanned for private markers |
| `.env` | Untouched; not required for public feeds |
| Runtime #1 | slug `btc-updown-5m-1784215500` (~45s) — books/reference initialized; all decisions `SKIP` (near-expiry one-sided books / early history) |
| Runtime #2 (primary) | slug `btc-updown-5m-1784215800` (~40s) — two-sided books |
| Market | Bitcoin Up or Down - July 16, 11:30AM-11:35AM ET |
| Condition | resolved via Gamma; YES/NO token IDs mapped from `clobTokenIds` + outcomes |
| Signal counts | UP 188, DOWN 353, FLAT 2010, UNAVAILABLE 1065 |
| Decision counts | WOULD_ENTER_UP 188, WOULD_ENTER_DOWN 353, HOLD 2010, SKIP 1065 |
| Freshness | Changes with clock/event age (not latched); UNAVAILABLE includes insufficient history |
| Initialization | `book_state_initialized` ×2, `reference_state_initialized` ×1 before useful decisions |
| Reconnect | Controlled force-close of PM WS emitted `reconnecting` → `book_state_invalidated` (books not silently kept); unit tests cover delta-without-snapshot + tick invalidate |
| Next-window | `discover-btc-window` / `next_btc_updown_slug` — same observe host, no second runtime path |
| Fact artifacts | `var/reporting/r3/live_observe_facts.jsonl`, `var/reporting/r3/live_observe_next_facts.jsonl` (gitignored) |

#### Acceptance checklist (R3B)

| Criterion | Result |
|-----------|--------|
| Correct market + YES/NO mapping | Pass |
| Books initialize before available decisions | Pass |
| Binance reference initializes | Pass |
| Raw payloads stay in adapters | Pass (facts are normalized summaries) |
| Freshness not latched | Pass |
| Missing/stale → UNAVAILABLE/SKIP | Pass |
| Signal + decision facts emitted | Pass |
| Graceful shutdown closes + flushes | Pass |
| Reconnect does not keep invalid book | Pass (invalidate on reconnecting) |
| Next-window not a second runtime | Pass |
| No private trading operation | Pass |

---

### Deviations

1. R2 checkpoint already existed before this session (`ccccc96`); not recreated.
2. Near-expiry current window produced one-sided books → all SKIP; next window used for directional evidence.
3. `SPREAD_TOO_WIDE` initially also covered one-sided books; corrected to `BOOK_ONE_SIDED_OR_EMPTY`.
4. Gamma HTTP requires `User-Agent` (403 without it).
5. `websockets` added as a runtime dependency for live adapters.

### Proposed R4 scope

See `07_implementation_plan.md` — intents, risk authorization, execution plan structures, fact wiring; no OMS submit, no portfolio, no Z-Gap.

**Stop before R4.**
