# G7 — Extra market data (Binance book / perp)

**Status:** Implemented for spot L2 + derived mid. Perp trades/L2/funding stay fail-closed.

**Phase:** Grounding fixes  
**Depends on:** none technically  
**Do not start** unless G8 (or another plugin) **needs** these facts. ask70 and z_gap do not.

## Goal

If a future strategy asks for Binance **spot book**, **perp trades/book**, or **funding**, the host should start a real adapter — or keep failing closed. Today the names exist; the pipes do not.

## Why this is a problem only later

z_gap already has: Polymarket books, Binance **spot trades**, Chainlink TWAP, clock, account, sealed PTB.  
ask70 has: Polymarket books, clock, account.

`Q_EDGE_INPUT_CONTRACT` also names:

| Fact id | Adapter today |
|---------|----------------|
| `binance.spot.l2` | **Missing** |
| `binance.spot.mid` | Derived from L2 — blocked |
| `binance.perp.trades` | **Missing** |
| `binance.perp.l2` | **Missing** |
| `binance.perp.mid` | Derived — blocked |
| `binance.perp.funding` | **Missing** |

`composition.missing_live_adapters()` ∩ start → fail closed. That is correct. Do not “fake” mid from last trade and call it L2.

## Venue / exchange facts (not Polymarket)

These feeds are **Binance public market data**, not CLOB.

- Spot L2: existing depth stream pattern (see current `binance.spot.trades` adapter). Need an **instrument-keyed** depth store — already sketched in `src/tyrex_pm/indicators/` (`DepthSnapshot` / `DepthStore`). That store is **not** the Polymarket `[0,1]` book.
- Perp: different symbol and endpoints (USDT-M). Funding is a periodic mark, not a trade tape.
- Do not start Binance connectors for ask70. Contract closure stays the law.

No Polymarket API change is required for this task.

## Current code

- `facts/ids.py` — ids frozen.
- `runtime/composition.py` — `UNIMPLEMENTED_ADAPTERS`.
- `indicators/` — OFI, imbalance, microprice, RV over generic depth.
- `adapters/binance/ws_adapter.py` — spot **trades** only.

## Required implementation (when a strategy needs it)

One adapter per source fact, same style as spot trades:

1. `binance.spot.l2` — WS depth → `DepthStore` keyed by `BTCUSDT` (or config symbol).
2. Producer `binance.spot.mid` from that store.
3. `binance.perp.trades` / `.l2` / `.funding` — only if G8 q-edge (or other) still requires them. Can ship spot L2 first if the first third strategy is spot-only.

Rules:

- Ingest ≠ evaluate. `EvaluationScheduler` wakes only on `evaluate_on`.
- Freshness/staleness blockers on the contract, same as books.
- Tests with recorded fixtures; no live Binance required for unit PASS.
- Move each fact out of `UNIMPLEMENTED_ADAPTERS` only when the adapter exists.

## Out of scope

- Using L2 inside z_gap or ask70 “because we built it”
- Polymarket L2 changes
- Paid Binance endpoints

## Acceptance

1. A contract that lists `binance.spot.l2` starts only that extra connector and runs without Binance perp.  
2. ask70 still starts **no** Binance.  
3. Missing remaining unimplemented facts still fail closed.  
4. Indicator unit tests read the new store, not `BookSnapshot`.
