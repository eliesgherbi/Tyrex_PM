# 07 — Implementation plan

**Branch:** `rest_project`  
**R1 checkpoint:** `630bac2acf67961a30b4be014d1df0434af967f1`  
**R2 checkpoint:** `ccccc969bb4877ae97e6e56c656b839739034425`  
**Status:** R3 complete (R3A fixtures + R3B public live); stop before R4

## Engine decision

Minimal Tyrex event-driven engine. NautilusTrader is not a dependency.

## Safe rollback

Explicit reverse of named `git mv` / targeted `git restore` / `git revert` of commits.  
Do **not** use `git checkout HEAD -- .` or `git clean -fd`.

## Roadmap

| Phase | Scope | Status |
|-------|--------|--------|
| R1 | Archive + skeleton | **Done** (`630bac2`) |
| R2 | Core contracts + dispatcher | **Done** (`ccccc96`) |
| R3 | Read-only MD + indicators + signal + observe strategy | **Done** |
| R4 | Intents + risk + planning | Next |
| R5 | Shadow OMS + portfolio | Planned |
| R6 | Live Polymarket OMS adapter | Planned |
| R7 | Tiny-live (auth) | Planned |
| R8 | Framework acceptance | Planned |
| Z1–Z4 | Z-Gap after R8 | Planned |

## R3 decisions (summary)

- Book protocol: **Option B** (snapshot + delta + tick_size); store owns reconstruction.
- Binance feed: public `btcusdt@trade` (individual trades; event time `T`).
- Freshness: derived at decision time; typed thresholds; no latched fresh flag.
- Momentum: \(P_t/P_{t-L}-1\); no interpolation; ignore out-of-order; duplicate ts replaces.
- Observe decisions only — no intents.
- One `ObserveHost` for fixture and live.
- Dependency: `websockets` for live adapters only.

## Proposed R4 scope (exact)

1. Immutable trading **intent** types (`EnterIntent`, `HoldIntent`, `FlattenIntent` / exit family as needed).
2. Strategy → intent mapping from observe decisions (still no live submit).
3. Risk authorization gate: mode, instrument allowlist, freshness, size/notional caps, spread/price bounds, kill switch, duplicate-in-flight guard.
4. Execution **plan** structures (not OMS yet): limit price policy, TIF, cancel/replace sketch.
5. Wire intents + risk results into facts; keep shadow/live submit for R5–R6.
6. No portfolio persistence, no order/fill truth, no Z-Gap/PTB.
