# Layer A — Rule 3: Significance conviction — median vs prior BUYs (gating)

**Status:** planning only.

**Rule kind:** **Gating** — answers *should this **entry** signal proceed?*

**Distinct from** `conviction_sizing_enabled` in `src/tyrex_pm/signal/sizing.py`: that policy **scales quantity after accept**. This rule **accepts or denies before sizing** using a **relative** USD notional benchmark.

---

## 1. Purpose

For guru **BUY** signals, **deny** when the trade’s **USD notional** is **not strictly greater** than the **median** notional of the **prior** **N** guru **BUY** trades (rolling window, BUY-only history).

---

## 2. Frozen v1 behavior (complete)

| Topic | Decision |
|--------|-----------|
| History composition | **BUY-only** — SELL signals **never** append and **never** appear in the deque. |
| What enters history | **Every** guru **BUY** for which **token allowlist passed** and **`notional_usd` is computable**, **including** BUYs **denied** by static amount or by this filter **after** processing. |
| Current trade vs median | Compare current BUY to median of **prior** BUY notionals **only** — current **excluded** from median calculation. |
| Cold start | **Zero** prior BUY samples → **pass** (cannot compute median). Emit DEBUG `significance_conviction_cold_start` once per cold evaluation (optional rate-limit not required v1). |
| Strict inequality | **Pass** only if `current_notional > median_prior`. If `current_notional == median_prior` → **deny** (“strictly above median”). |
| Even sample size | Use **`statistics.median`** from Python stdlib (for even *n*, average of two middle values). **Document** this for operators. |
| `threshold_method` | Loader allows **`median`** only in v1; any other value → **`ValueError`**. |
| Persistence | **In-memory only** — restart clears deque. |

---

## 3. Config (v1)

```yaml
filters:
  significance_filter:
    significance_conviction:
      enabled: false
      lookback_trades: 20
      threshold_method: median
```

| Field | Validation |
|--------|------------|
| `enabled` | bool |
| `lookback_trades` | int **`>= 1`** when enabled |
| `threshold_method` | literal **`median`** when enabled |

---

## 4. Notional

Same as Rule 2: shared **`notional_usd(sig) -> float | None`** in `layer_a/notional.py`.

- If **`None`** for **current** signal → **deny** with `LAYER_A_SIGNIFICANCE_NOTIONAL_MISSING` (same component cases as static filter — may reuse codes or split; **implementation:** one canonical “missing notional” code for conviction).

---

## 5. Algorithm (implementation spec)

**Data structure:** `collections.deque[float]` with **`maxlen = lookback_trades`**, storing **BUY notional** values in **chronological** order (oldest evicted when full).

**On each guru BUY that passed token gating:**

1. Compute `current = notional_usd(sig)` (may be `None`).
2. **Evaluate** median gating **only if** `significance_conviction.enabled`:
   - If `current is None` → **deny** (see §6 — no append).
   - Snapshot `prior = list(deque)` (deque unchanged).
   - If `len(prior) == 0` → **pass** (cold start).
   - Else `m = statistics.median(prior)`.
   - If `current > m` → **pass**; else **deny** with `LAYER_A_DENY_SIGNIFICANCE_MEDIAN`.
3. **`observe_buy` (post-step, after full entry Layer A chain):** if `current is not None`, **`deque.append(current)`** — **even** if static filter denied (median step skipped) or this filter denied.

**`significance_conviction.enabled: false`:** no median evaluation and **no** `observe_buy` (deque unused).

**Non-BUY signals:** no evaluation and no deque mutation.

**Token gating failed:** no `observe_buy` and no significance evaluation.

**Ordering contract:** `observe_buy` runs **after** static + significance steps for this signal, so the **median** for the current evaluation uses deque contents **before** `observe_buy` appends the current notional.

**Clarification:** When static filter **denies**, orchestrator **short-circuits** before calling `SignificanceConvictionFilter.evaluate`, but **still** calls `observe_buy` when `current is not None` so the BUY-only history includes trades that failed the static floor.

---

## 6. Deny without append?

- If `current is None`: **deny**; **do not** append (nothing to store).
- If `current` is valid: **always append after chain** (per §5), regardless of static/significance deny.

---

## 7. Applicability

- **Entry / BUY** only for evaluation.
- **Exit:** filter not registered.

---

## 8. State

| Item | Owner |
|------|--------|
| `deque` of floats | `SignificanceConvictionFilter` instance (one per strategy / process) |
| Persistence | None v1 |

---

## 9. Reason codes (proposed)

| Code | When |
|------|------|
| `LAYER_A_SIGNIFICANCE_NOTIONAL_MISSING` | Missing/invalid price or size for current BUY |
| `LAYER_A_DENY_SIGNIFICANCE_MEDIAN` | `current <= median_prior` |
| `LAYER_A_SIGNIFICANCE_OK` | Pass including cold start |
| `LAYER_A_SIGNIFICANCE_COLD_START` | Optional **detail** / DEBUG only — not required as `reason_code` on pass (use OK + log) |

---

## 10. Module layout

| Piece | Path |
|--------|------|
| Filter + deque | `src/tyrex_pm/signal/layer_a/filters/significance_conviction.py` |
| `observe_buy` | Same class, called by **`LayerAOrchestrator`** after entry chain |

---

## 11. Logging

- **Deny:** INFO with `current_notional`, `median_prior`, `window_len_prior`.
- **Cold start pass:** DEBUG.
- **Pass (with median):** DEBUG optional.

---

## 12. Reporting

- **`layer_a_filter`:** `filter_name=significance_conviction`, `branch=entry`, `accept`, `reason_code`, `metadata` with `current_notional`, `median_prior` (if applicable), `lookback_trades`, `window_len_prior`.

---

## 13. Implementation checklist

1. [ ] `SignificanceConvictionSettings` + loader.
2. [ ] `ReasonCode` entries.
3. [ ] Implement deque + `statistics.median` + cold start.
4. [ ] Orchestrator: order static → significance; post-chain `observe_buy`.
5. [ ] Unit tests §14.
6. [ ] Integration: static denies but deque still grows.

---

## 14. Test matrix (required)

| # | Scenario | Expect |
|---|----------|--------|
| A1 | Empty deque, enabled, valid notional | **pass** (cold start); after observe, deque len 1 |
| A2 | Prior `[10, 20, 30]`, current `25` | median20, pass (`25 > 20`) |
| A3 | Prior `[10, 20, 30]`, current `20` | deny (`20 > 20` false) |
| A4 | Prior `[10, 20, 30, 40]` — median `(20+30)/2=25`, current `25` | deny (strict) |
| A5 | Prior `[10, 20, 30]`, current `20.0000001` vs median 20 | pass |
| A6 | `maxlen=3`, append 4th | oldest evicted |
| A7 | Static denies, significance enabled, valid notional | observe still appends; next BUY sees updated deque |
| A8 | Token deny | no observe |
| A9 | `current None` | deny, no append |
| A10 | Loader `threshold_method: mean` | **ValueError** |

---

## 15. Worked example (operator mental model)

- `lookback_trades: 3`, prior BUY notionals in deque: `[100, 400, 900]` → sorted for median: `[100,400,900]`, median **400**.
- Current BUY notional **401** → **pass** (`401 > 400`).
- Current **400** → **deny**.
- After pass/deny with computable current **401**, deque becomes `[400, 900, 401]` (evict **100** if maxlen 3) — **exact eviction** depends on chronological order; deque stores **append order**, median computed on **values** in deque **before** append of current (implementation: snapshot **before** `observe_buy`).

---

## 16. Future extensions

- Percentile, mean, z-score; per-token windows; persisted history — **not** v1.
