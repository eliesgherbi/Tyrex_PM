# NautilusTrader assessment and PoC decision record

**Status:** PoC specification complete — **engine decision PENDING**  
**Pinned stable release:** `nautilus_trader==1.230.0`  
**Release date:** 2026-06-29 (UTC)  
**Git tag:** `v1.230.0`  
**Annotated tag object:** `112d335088ec11cdd1d60038b16c8fe56406aead`  
**Peeled commit:** `8160730c7c550480b0a439fb11086a4c4de15f0b`  
**PyPI:** https://pypi.org/project/nautilus_trader/1.230.0/  
**GitHub release:** https://github.com/nautechsystems/nautilus_trader/releases/tag/v1.230.0  
**Docs (latest guide; verify against 1.230.0 sources):** https://nautilustrader.io/docs/latest/integrations/polymarket/  

**Selected adapter surface for PoC:** Rust-native Polymarket path exposed through Python (`nautilus_trader.adapters.polymarket`), which is the consolidation target per official docs. Python-only options are noted where they differ.

**Develop branch:** Informational only. Unreleased features are **not** treated as available.

---

## 1. Capability matrix (pinned to 1.230.0)

Classification key:

| Label | Meaning |
|-------|---------|
| Stable verified | In 1.230.0 release notes and/or docs matching stable adapter |
| Stable + adaptation | Present but needs Tyrex config/policy wrapping |
| Develop-only | Seen on develop/RELEASES unreleased notes — **not** counted as available |
| Python-only | Documented as Python adapter option |
| Rust-only / Rust-primary | Rust adapter surface / config |
| Docs not source-verified | Documented; local source tree of 1.230.0 not fully checked out in this repo |
| Live proof required | Must be demonstrated in PoC |
| Unsupported | Not available for our need |

| Capability | 1.230.0 status | Notes / references |
|------------|----------------|--------------------|
| BinaryOption YES/NO instruments | Stable + adaptation | Docs: Binary options / instrument provider |
| Gamma discovery / auto-load | Stable + adaptation | Docs: Runtime instrument loading |
| Up/Down event slug builder | Stable + adaptation / Docs not source-verified at pin | Docs: `PolymarketUpDownEventSlugConfig` |
| CLOB L2 / quotes / trades WS | Stable verified | Docs WebSockets; live proof required for parity |
| Tick-size change book epoch | Stable + adaptation | Docs: Tick size change handling |
| LIMIT / MARKET orders | Stable verified | Docs Orders capability |
| TIF GTC/GTD/FOK/IOC→FAK | Stable verified | Docs Time-in-force |
| Order amend | Unsupported | Cancel-only |
| Bracket / stop / OCO | Unsupported | Venue + adapter |
| Partial fills + dust snap | Stable + adaptation | Docs Fill quantity normalization; live proof required |
| Reconciliation / restart | Stable + adaptation | Docs Reconciliation; 1.230.0 fix: “Polymarket reconciliation producing out-of-range fill prices” |
| Category fee model | Stable + adaptation | Docs Fees / `PolymarketFeeModel` — **not** Tyrex `fd` curve |
| Dynamic `fd` fee from CLOB markets | Unsupported in NT | Remains Tyrex `quant/fees.py` |
| Market resolution → InstrumentClose | Stable + adaptation | Docs Market resolution events; **no redeem** |
| Historical trades loader | Stable + adaptation | Docs Historical data loading |
| Historical CLOB book snapshots | Unsupported | Docs: loader does not expose book history |
| Binance data client coexistence | Stable + adaptation | Separate Binance integration; live proof for joint clocking |
| RTDS `crypto_prices` custom data | Stable verified (in 1.230.0) | Release notes: RTDS added earlier (#4214); 1.230.0 fixes RTDS duplicate snapshot (#4319) |
| RTDS `crypto_prices_chainlink` | Unsupported / Live proof | Tyrex uses Chainlink topic (`venue/polymarket_rtds/normalize.py`). NT public types documented as `PolymarketRtdsCryptoPrice` / Equity — **Chainlink PTB not assumed native** |
| PTB boundary capture / attestation | Unsupported | Tyrex domain service |
| Custom data extensibility | Stable + adaptation | CustomData / subscribe_data path |
| Multi-leg atomic contingency | Unsupported | Strategy-level saga only (Track E) |
| Risk engine | Stable + adaptation | Different policy model than Tyrex reason codes |
| Backtest engine shell | Stable + adaptation | Limited by book-history gap |
| Persistence / cache recovery | Stable + adaptation | Live proof required |
| Observability = facts.jsonl | Unsupported | Bridge required if NT adopted |

**Python vs Rust (stable docs):** Signing, some exec recovery flags (`generate_order_history_from_trades`), and some data config knobs differ. PoC standardizes on the **Rust-primary Python bindings** unless a blocker forces a documented Python-only option.

---

## 2. Engine decision options (exactly one)

At PoC end, choose one:

1. **Adopt NautilusTrader** as generic foundation; Tyrex keeps domain + Z-Gap + ops + facts bridge.  
2. **Retain and simplify Tyrex spine**; NT rejected or deferred.  
3. **Temporary migration bridge** with **removal date** and exit criteria (not a permanent dual engine).

Current choice: **PENDING**.

---

## 3. PoC tracks (required)

All tracks pin `nautilus_trader==1.230.0`. Record raw artifacts under `var/reporting/z_gap_nt_poc/` (to be created when PoC runs).

### Track A — Market-data equivalence

**Objective:** Same BTC 5m market, compare NT vs Tyrex Path A books.

**Compare:** instrument/token mapping, best bid/ask, L2 depth, timestamps, freshness, sequence continuity, tick-size changes, executable size/VWAP (Tyrex `sweep_vwap` vs NT-derived), reconnect behavior.

**Predeclared tolerances (draft — freeze before run):**

| Metric | Tolerance |
|--------|-----------|
| Best bid/ask price | ≤ 1 tick |
| Top-N depth size (N=5) | ≤ 2% relative or 1 share abs |
| Event time vs Tyrex book ts | ≤ 250 ms when both healthy |
| Freshness classification | Same bucket when both connected |
| After reconnect | Full snapshot reseeds; no silent strategy reset |

**Pass:** Tolerances held for ≥1 full window + one forced reconnect.  
**Fail:** Persistent token mismatch, missing snapshots, or diverging prices beyond tolerance while both healthy.

### Track B — Order lifecycle and recovery

**Objective:** Tiny live (≤ $5) single-leg on Polymarket via NT.

**Steps:** submit LIMIT → ack → partial fill if safe → cancel remainder → restart with open or recently filled order → reconcile → flatten residual.

**Pass/fail:**

| Criterion | Pass |
|-----------|------|
| Reconciliation time | ≤ 30 s after restart to agree with venue |
| Position discrepancy | 0 beyond dust (≤ 0.01 shares per NT dust policy) |
| Duplicate/missing fills | None after idempotent apply |
| Stuck order | None undetected > 60 s |
| Cancel / flatten | Residual flat within 60 s of flatten submit |

**Fail:** Lost fills, false reject of filled order, or inventory disagreeing after restart.

### Track C — Tyrex custom-data integration

**Objective:** Binance + Chainlink/RTDS + PTB + clock-quality enter **one** event model without a second runtime authority, second event loop, second order/position state, conflicting timestamps, or second operator path.

**Native NT PTB not required.** Clean extension (CustomData actor or Tyrex sidecar feeding NT Strategy) is required.

**Pass:** Single host process; one portfolio/order authority; PTB lock usable by observe evaluation; timestamps monotonic under shared clock policy.  
**Fail:** Second OMS/portfolio, or PTB/clock forked from strategy loop.

### Track D — Z-Gap observe slice

**Objective:** Smallest read-only vertical slice:

```
engine data → normalized Z-Gap context → σ → FV → fee edge → entry_eval → observe fact
```

Compare to current `run_observe_tick` / `evaluate_z_gap_entry` on identical inputs (replayed snapshots or dual-read).

**Pass:** Decision fields match within frozen tolerances (z, fv, edge, would_enter, skip reasons).  
**Fail:** Systematic math or gate divergence with same inputs.

### Track E — Multi-step execution feasibility (design + spike)

**Objective:** Confirm NT order/fill/recovery events suffice for a future pair saga **without** bypassing engine state. **Do not migrate Paired Binary.**

**Pass:** Written spike shows saga can drive two legs + unwind using only engine events + strategy state.  
**Fail:** Saga requires a second order ledger or direct venue calls.

---

## 4. PoC execution order

1. Freeze tolerances (A/D).  
2. A (read-only) + C scaffolding.  
3. D on captured/identical data.  
4. B (requires explicit live authorization).  
5. E spike (can parallelize with D).  
6. Write decision + evidence into this file.

## 5. Rejection criteria (NT)

Reject adoption (choose simplify Tyrex) if any of:

- Track B fail on restart/fill truth  
- Track C fail (cannot integrate Chainlink/PTB without second authority)  
- Track A fail on instrument/book identity  
- Track E shows coordinated execution impossible without bypassing engine

## 6. Decision log

| Date | Event | Result |
|------|-------|--------|
| 2026-07-16 | Pin 1.230.0; write PoC tracks | Decision PENDING |
| | Track A | Not run |
| | Track B | Not run |
| | Track C | Not run |
| | Track D | Not run |
| | Track E | Not run |
| | **Final engine choice** | **PENDING** |

---

## 7. Implications for refactoring (until decision)

**Allowed now:**

- Document invariants and ownership
- Unify Z-Gap control-path *design*
- Extract pure functions already under `strategies/z_gap` / `quant` behind clearer hosts
- Add architecture tests that encode dependency rules (without deleting live paths)

**Not allowed yet:**

- Committing the codebase to NT types as the only portfolio/order model
- Deleting Tyrex OMS/reconcile paths
- Tiny-live on a new NT path without Track B pass + explicit authorization
