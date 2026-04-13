# Layer A — Rule 2: Static amount significance (gating)

**Status:** planning only.

**Rule kind:** **Gating** — answers *should this **entry** signal proceed?*

---

## 1. Purpose

For guru **BUY** signals, **deny** when **guru trade notional (USD)** is **strictly below** a configured floor. Improves copy quality and fee economics without reference to guru history.

---

## 2. Applicability

- **`branch == "entry"`** (`side == "BUY"`) only.
- **`branch == "exit"`:** filter **does not apply** — orchestrator **skips** this filter (or filter returns **`accept=True`** with `reason_code` indicating no-op — **implementation:** prefer **skip registration** on exit pipeline for clarity).

---

## 3. Config (v1)

```yaml
filters:
  significance_filter:
    static_amount:
      enabled: false
      amount_usd: 700.0
```

| Field | Validation |
|--------|------------|
| `enabled` | bool; default `false` |
| `amount_usd` | If `enabled: true`, **required**, **`float > 0`** — else **loader `ValueError`** |

---

## 4. Notional definition (frozen)

Use **`GuruTradeSignal`** fields (`src/tyrex_pm/core/types.py`):

\[
\text{notional\_usd} = \text{float}(\text{price\_raw}) \times \text{float}(\text{size\_raw})
\]

- Coerce with **`float(...)`** after None checks.
- **No** rounding beyond float semantics v1.

- **Pass:** `notional_usd >= amount_usd`
- **Deny:** `notional_usd < amount_usd`

---

## 5. Edge cases (frozen)

| Case | `accept` | `reason_code` (proposed) | `detail` |
|------|----------|---------------------------|----------|
| `price_raw is None` | False | `LAYER_A_STATIC_AMOUNT_PRICE_MISSING` | empty or `"price_raw null"` |
| `size_raw is None` | False | `LAYER_A_STATIC_AMOUNT_SIZE_MISSING` | empty or `"size_raw null"` |
| `price_raw <= 0` | False | `LAYER_A_STATIC_AMOUNT_INVALID_PRICE` | include raw |
| `size_raw <= 0` | False | `LAYER_A_STATIC_AMOUNT_INVALID_SIZE` | include raw |
| `notional_usd < amount_usd` | False | `LAYER_A_DENY_STATIC_AMOUNT_BELOW_THRESHOLD` | threshold + computed |
| `notional_usd >= amount_usd` | True | `LAYER_A_STATIC_AMOUNT_OK` | optional |

**Loader:** `amount_usd <= 0` when enabled → **`ValueError`**.

---

## 6. State

- **Stateless** — no deque, no files.

---

## 7. Reason codes

Register all §5 codes in **`core/reason_codes.py`** (prefix may be adjusted to match repo convention; use **stable** strings for facts).

---

## 8. Module layout

| Piece | Path |
|--------|------|
| Filter | `src/tyrex_pm/signal/layer_a/filters/static_amount.py` |
| Shared notional helper | `src/tyrex_pm/signal/layer_a/notional.py` (imported by Rule 3) |

---

## 9. Orchestrator placement

- **After** token allowlist, **before** significance conviction (`00_general_plan.md` §6).
- On deny: **short-circuit** entry pipeline; **still** run **`SignificanceConvictionFilter.observe_buy`** if significance conviction is enabled (BUY + token passed + computable notional — see `00_general_plan.md` §6 and `03` doc).

---

## 10. Logging

- **Deny:** INFO `event=copy_skip` + final reason; DEBUG optional with `notional_usd`, `amount_usd`.

---

## 11. Reporting

- **`layer_a_filter`:** `filter_name=static_amount`, `branch=entry`, `accept`, `reason_code`, `detail`, `metadata` e.g. `{"notional_usd": ..., "threshold_usd": ...}` (JSON-safe floats).
- **`strategy_decision`:** skip when denied.

---

## 12. Implementation checklist

1. [ ] `StaticAmountSettings` + loader under `filters.significance_filter.static_amount`.
2. [ ] `notional_usd(sig) -> float | None` helper (None if missing/invalid components).
3. [ ] `StaticAmountGatingFilter.evaluate(...)` → `LayerAOutcome`.
4. [ ] Register on **entry** pipeline only.
5. [ ] `ReasonCode` entries.
6. [ ] Unit tests §13.
7. [ ] Facts shape in reporting tests.

---

## 13. Test matrix (required)

| # | Input | Expect |
|---|--------|--------|
| T1 | `price_raw=0.5`, `size_raw=2000`, threshold `700` | **pass** (`notional=1000 >= 700`) |
| T2 | `price_raw=0.5`, `size_raw=1000`, threshold `700` | **deny** (`notional=500 < 700`) |
| T3 | `price_raw=None` | deny PRICE_MISSING |
| T4 | `size_raw=None` | deny SIZE_MISSING |
| T5 | `price_raw=-1`, `size_raw=10` | deny INVALID_PRICE |
| T6 | `price_raw=0.5`, `size_raw=0` | deny INVALID_SIZE |
| T7 | boundary `notional == amount_usd` | **pass** (`>=`) |
| T8 | `notional == amount_usd - epsilon` | deny BELOW_THRESHOLD |
| T9 | loader `amount_usd: 0`, enabled | error |

---

## 14. Future extensions

- Per-token thresholds; mark price from book (crosses runtime — not v1).
