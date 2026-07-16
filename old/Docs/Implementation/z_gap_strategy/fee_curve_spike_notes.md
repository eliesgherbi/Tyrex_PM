# Fee Curve Spike Notes (A0.0.a)

**Status:** Complete (2026-07-09)  
**Milestone:** Z-Gap Phase A — A0.0.a  
**Parent:** [`z_gap_strategy_v0_pahseA.md`](z_gap_strategy_v0_pahseA.md)

---

## Executive finding

**Outcome category: 1 — Full dynamic fee curve available**

Polymarket exposes per-market dynamic taker fee parameters on `/clob-markets/<condition_id>` in field `fd`. The installed SDK (`py_clob_client_v2`) already implements the fee formula. Tyrex_PM `MarketInfo` stores the full raw response but **does not parse `fd` today**; it only surfaces flat `fee_rate_bps` from `/fee-rate` (`base_fee`), which is **not** sufficient for Z-Gap edge math.

**A0.4 plan:** Implement `quant/fees.py` using `fd.r` + `fd.e` from `MarketInfo.raw` (or extend `MarketInfo` with parsed `FeeDetails`). **Do not** use `fee_rate_bps / 10000 * price` as trusted edge logic.

**`entry_mode: enforce` gate:** Allowed after `phi(price)` is wired and validated on tiny fills (`fee_validation` facts). Observe_only does not require fee validation on real fills.

---

## 1. Does live market info expose the dynamic fee curve?

**Yes.** Field `fd` on `/clob-markets/<condition_id>`.

### Live evidence (BTC 5m window `btc-updown-5m-1783628700`, 2026-07-09)

| Endpoint | Status | Key payload |
|----------|--------|-------------|
| `GET https://clob.polymarket.com/clob-markets/0x507f97b1...` | 200 | See below |
| `GET https://clob.polymarket.com/fee-rate?token_id=<yes>` | 200 | `{"base_fee": 1000}` |

**`/clob-markets` response keys (observed):**

```text
ao, aot, c, fd, itode, mbf, mos, mts, r, t, tbf
```

**`fd` object (live):**

```json
{
  "r": 0.07,
  "e": 1,
  "to": true
}
```

| Field | Meaning (SDK + live sample) |
|-------|----------------------------|
| `fd.r` | Fee rate coefficient (float, e.g. `0.07`) |
| `fd.e` | Fee exponent (float/int, e.g. `1`) |
| `fd.to` | Taker-only flag (`true` for BTC 5m sample) |
| `mbf` | Maker base fee bps (`1000` observed) |
| `tbf` | Taker base fee bps (`1000` observed) |

---

## 2. What does Tyrex_PM store today?

**File:** `src/tyrex_pm/venue/polymarket/market_info.py`

| Source | Stored? | Used for fees? |
|--------|---------|----------------|
| `/clob-markets/<condition_id>` full JSON | **Yes** → `MarketInfo.raw` | **No** — `fd` not parsed |
| `mts`, `mos`, `t` | Yes → structured fields | Tick / min size |
| SDK `get_fee_rate_bps(token_id)` | Yes → `MarketInfo.fee_rate_bps` | **Only flat `base_fee`** |

`get_fee_rate_bps` hits `GET /fee-rate?token_id=` and returns `base_fee` (live: `1000` bps). This is a **separate flat endpoint**, not the dynamic curve.

**Gap:** Z-Gap edge requires `fd.r` and `fd.e` from `raw["fd"]` or SDK `get_clob_market_info` / `get_fee_exponent`.

---

## 3. Is `phi(price)` directly computable?

**Yes.** Formula is implemented in the installed SDK:

**File:** `py_clob_client_v2/fees.py` (site-packages, version bundled with `tyrex-pm[live]`)

```python
platform_fee_rate = fee_rate * (price * (1 - price)) ** fee_exponent
```

**Parameters** loaded from `fd` in `py_clob_client_v2/client.py` → `get_clob_market_info()`:

```python
fd = result.get("fd") or {}
self.__fee_infos[token_id] = FeeInfo(
    rate=fd.get("r", 0.0),
    exponent=fd.get("e", 0.0),
)
```

### Proposed Z-Gap `phi(price)` per share (taker buy)

For edge in probability units (binary pays $1):

```text
phi_taker_fee_per_share(price) = fd.r * (price * (1 - price)) ** fd.e
```

**Live sample** (`fd.r=0.07`, `fd.e=1`):

| price | `phi` (platform_fee_rate per share, USDC) |
|-------|-------------------------------------------|
| 0.10 | 0.0063 |
| 0.50 | 0.0175 (peak) |
| 0.90 | 0.0063 |

Curve peaks near 0.50 as expected for `(p*(1-p))^1`.

**Edge usage (A0.4 / A0.6):**

```text
edge_UP = p_UP - ask_UP - phi(ask_UP) - slippage_UP
```

**Model-capped limit (A0.6):** solve `p_L - limit - phi(limit) - slippage >= theta_fill_floor` via `max_fill_price_for_edge_floor(...)`.

---

## 4. Is only `fee_rate_bps` / flat fee available?

**Both exist, but they are different things:**

| API | Field | Live BTC 5m value | Use for Z-Gap edge? |
|-----|-------|-------------------|---------------------|
| `/fee-rate` | `base_fee` | `1000` bps | **No** — flat; used for V1 order `feeRateBps` signing |
| `/clob-markets` | `fd.r`, `fd.e` | `0.07`, `1` | **Yes** — dynamic curve |

Using `fee_rate_bps=1000` as `price * 0.10` would **overstate** fees vs dynamic curve at typical asks (e.g. 0.45 → phi ≈ 0.0086 vs flat 0.045).

---

## 5. Fallback paths if `fd` missing

| Condition | Fallback | Enforce gate |
|-----------|----------|--------------|
| `fd` present with `r` and `e` | Implement real `phi(price)` | Observe_only → tiny fills → `fee_validation` → enforce |
| `fd` absent but `fee_rate_bps` only | Document as flat fallback; mark `fee_model_id: flat_bps_fallback` | **Block enforce** until ≥3 empirical `fee_validation` facts |
| Cannot fetch market info | `z_gap_fee_model_unknown` | **Block enforce**; observe_only only |

---

## 6. What should block `entry_mode: enforce`?

Until A0.8 checklist:

1. `fd` parsed and `phi(price)` implemented (A0.4)
2. Observe_only windows reviewed (A0.5)
3. Fresh PTB attestation ≤ 0.5 bps
4. Clock drift ≤ 500 ms
5. Binance connectivity pass (A0.0.b)
6. After first fills: `fee_validation` predicted vs observed within tolerance

---

## 7. Implementation recommendations for A0.4

1. **Extend `MarketInfo`** (preferred) or add `quant/fees.py` parser on `MarketInfo.raw`:

   ```python
   @dataclass(frozen=True)
   class PolymarketFeeCurve:
       rate: Decimal      # fd.r
       exponent: Decimal  # fd.e
       taker_only: bool   # fd.to
       fee_model_id: str = "polymarket_fd_v1"

   def phi_taker_fee_per_share(price: Decimal, curve: PolymarketFeeCurve) -> Decimal:
       ...
   ```

2. **Fail closed** if `raw.get("fd")` missing or `r`/`e` not parseable in live mode.

3. **Emit fact** `fee_model_resolved` with `fee_model_id`, `fd_r`, `fd_e`, `fd_to`, sample `phi_at_ask`.

4. **Keep** `fee_rate_bps` for OMS/V1 compatibility only — not for edge.

5. **Post-fill** `fee_validation` fact after first enforce fills (A0.8).

---

## 8. Evidence references (repo)

| Artifact | Path |
|----------|------|
| Tyrex market info adapter | `src/tyrex_pm/venue/polymarket/market_info.py` |
| CLOB host default | `src/tyrex_pm/venue/polymarket/clob_env.py` → `DEFAULT_CLOB_HOST_V2` |
| SDK fee formula | `py_clob_client_v2/fees.py` |
| SDK fd parsing | `py_clob_client_v2/client.py` → `get_clob_market_info()` |
| SDK types | `py_clob_client_v2/clob_types.py` → `FeeDetails`, `FeeInfo` |
| Test fake clob response | `tests/test_market_info_cache.py` (no `fd` in fake — extend in A0.4 tests) |

---

## 9. Spike conclusion

| Question | Answer |
|----------|--------|
| Dynamic curve in `MarketInfo.raw`? | **Yes** — `fd` object |
| Full formula in SDK? | **Yes** |
| `phi(price)` computable? | **Yes** |
| `fee_rate_bps` alone sufficient? | **No** |
| Outcome category | **1 — Full curve available** |
| Proceed to A0.1? | **Yes** |

---

*Completed as part of Z-Gap A0.0. Next: `scripts/preflight_binance_connectivity.py` (A0.0.b).*
