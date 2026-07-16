# 07 — Implementation plan

**Branch:** `rest_project`  
**R1 checkpoint:** `630bac2acf67961a30b4be014d1df0434af967f1`  
**Status:** R2 complete; R3 not started

## Engine decision

Minimal Tyrex event-driven engine. NautilusTrader is not a dependency.

## Safe rollback

Explicit reverse of named `git mv` / targeted `git restore` / `git revert` of commits.  
Do **not** use `git checkout HEAD -- .` or `git clean -fd`.

## Roadmap

| Phase | Scope | Status |
|-------|--------|--------|
| R1 | Archive + skeleton | **Done** (`630bac2`) |
| R2 | Core contracts + dispatcher | **Done** |
| R3 | Read-only MD + indicators + signal + observe strategy | Next |
| R4 | Intents + risk + planning | Planned |
| R5 | Shadow OMS + portfolio | Planned |
| R6 | Live Polymarket OMS adapter | Planned |
| R7 | Tiny-live (auth) | Planned |
| R8 | Framework acceptance | Planned |
| Z1–Z4 | Z-Gap after R8 | Planned |

## R2 decisions (summary)

- Packages: `core/`, `engine/` only (plus existing `application/`).
- Intents deferred to R4.
- Execution events deferred to R5.
- Full book snapshots (not deltas).
- Exact-type dispatcher routing; queued reentrant publish; fail-fast errors.
- Decimal for trading values; no venue rounding yet.

## Proposed R3 scope

Polymarket discovery + book adapter; Binance reference adapter; market/reference state owners; freshness; executable views (incl. VWAP derive); momentum/mid/spread indicators; `DirectionalSignal` builder; observe-only `ReferenceMomentumStrategy`; timer/window host; fact JSONL sink; one runtime host wiring the R2 dispatcher. No OMS/risk intents.
