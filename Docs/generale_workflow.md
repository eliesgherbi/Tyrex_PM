
Here is a **simple, step-by-step** picture of what happens from “guru did something” to “we try (or don’t try) an order,” grounded in how Tyrex is wired.

---

## 1. The big picture (one sentence)

**Outside world → Tyrex turns a guru trade into a `GuruTradeSignal` → `CopyStrategy` decides “follow or skip,” sizes it, **risk** says “ok or no,” then **execution** sends a real order (live) or logs only (shadow).**

Think of it as **a chain of gates**. Each gate can say **stop** or **continue**.

---

## 2. Where each piece lives (modules)

| Stage | What happens | Main code area |
|--------|----------------|----------------|
| A. Guru data in | REST poll + optional RTDS stream → one normalized event | `data/` (e.g. `GuruMonitorActor`, `GuruStreamActor`, pipeline) |
| B. Signal on the bus | “Guru bought/sold this token, this size, this price” | `GuruTradeSignal` → internal bus topic |
| C. Strategy | Follow logic, sizing, min-notional check | `strategy/copy_strategy.py` + `signal/` (entry/exit, sizing) |
| D. Risk | Caps, gates, reserve, portfolio limits | `risk/configured.py` (injected as `RiskPolicy`) |
| E. Execution | Build/submit order to Polymarket via Nautilus | `execution/nautilus_guru_exec.py` (`NautilusGuruExecutionPort`) |

Reporting (if on) records facts **around** these steps; it does not decide trades.

---

## 3. The sequence (algorithm in plain English)

We walk through **one guru BUY** (same ideas apply to SELL with the exit branch).

### Step A — From market activity to “one guru trade”

- **GuruMonitorActor** asks Polymarket’s **Data API** on a timer: “any new activity for this wallet?”
- **GuruStreamActor** (if you use RTDS) listens to the **websocket** for the same kind of events.
- Duplicates are reduced; events are turned into a **`GuruTradeSignal`** (token, side BUY/SELL, size, price, ids).

**Outcomes**

- **No new guru trades** → no signal; nothing downstream runs. *Normal quiet time.*
- **Bad / wrong guru address** → effectively **no matching signals** for your guru. *Looks “dead” even if the app is fine.*

---

### Step B — `CopyStrategy` receives the signal

File flow: `_on_guru_trade` → `_handle_branch` with **entry** (BUY) or **exit** (SELL).

---

### Step C — Entry policy: “Should we even try to copy this trade?”

**Module:** `signal/entry.py` (`GuruFollowEntryPolicy`), plus **token filter** from your YAML.

Checks things like: wrong side handling, **token filter** (if enabled: only certain tokens).

**Outcomes**

- **Skip** → `decision.accept = false` (e.g. token not allowed). Log `copy_skip`, **stop.** No sizing, no risk, no order.
- **Accept** → continue.

*Example:* Filter allows only token `ABC`; guru trades `XYZ` → **skip** at this step.

---

### Step D — Sizing: “How big should *our* trade be?”

**Module:** `signal/sizing.py` (`SizingPolicy`).

Rough idea:

- Start from **guru size** (shares/contracts).
- Apply **`copy_scale`** (e.g. `0.08` → you want 8% of guru’s size intent).
- If **conviction sizing** is on, multiply by a **conviction factor** (from recent guru activity), capped by **`conviction_sizing_cap`**.

You get a **quantity** `qty`.

**Outcomes**

- **`qty ≤ 0`** → **skip** (`zero_qty`). **Stop.**
- **`qty > 0`** → continue.

*Example (simple, no conviction):*

- Guru bought **100** shares at ~$0.50.
- `copy_scale = 0.08` → target **8** shares (100 × 0.08).

---

### Step E — Build `OrderIntent` and call **risk**

**Module:** `risk/configured.py` (`ConfiguredRiskPolicy`).

Risk checks **your** YAML: per-order **min/max deploy** (`min_notional_usd_per_order` + `min_notional_policy`, `max_notional_usd_per_order` + `max_notional_policy`), token cap, portfolio cap, concurrent orders, capital gate, reserve, etc. It uses **live** state (cache/portfolio) when in live mode. **Default:** max deploy **`cap`** (clip qty down); min deploy **`deny`** (reject tiny BUYs unless you set **`min_notional_policy: cap`** to bump qty up).

**Outcomes**

- **Denied** → `approved = false`, log `risk_denied` with a **reason code** (e.g. per-order deny policy, portfolio cap, insufficient collateral after reserve). **Stop before execution.**
- **Approved** → may carry a **risk-adjusted** quantity (clipped or bumped); execution uses that intent.

*Example (tuned risk / small follower):*

- `max_notional_usd_per_order = 2`, **`max_notional_policy: deny`**, price × qty would be **$3** → **risk denies**.
- With **`max_notional_policy: cap`** (common default in templates) → **risk approves** with qty scaled so deploy ≤ **$2**.

---

### Step F — Execution (live vs shadow)

**Module:** `execution/nautilus_guru_exec.py` (`NautilusGuruExecutionPort`).

- **`execution_mode: shadow`** → **`NoOpExecutionPort`**: no real order; you may still see **`shadow_order_intent`** in logs.
- **`execution_mode: live`** → builds a **limit order** (with venue rules, min notional, etc.) and **`submit_order`** via Nautilus.

**Outcomes**

- **Venue accepts** → order lifecycle / fills in logs and reporting.
- **Venue rejects** (min size, no book, etc.) → you see errors / denied events; **not** the same as “risk approved.”

---

## 4. One full numeric example (BUY)

Assume:

- Guru **BUY**, **100** shares, **price ≈ $0.40** (guru notional ≈ **$40**).
- `copy_scale = 0.08`, `max_notional_usd_per_order = 2`, **`max_notional_policy: cap`** (default), conviction **off**.

| Step | Calculation | Result |
|------|-------------|--------|
| Sizing | `100 × 0.08 = 8` shares | `qty = 8` |
| Risk per-order | `$0.40 × 8 = $3.20` vs cap **$2** | **Clip** → deploy **$2** (e.g. **5** shares), then continue if other gates pass |

Same guru trade if you had **`copy_scale = 0.05`**:

- `qty = 5`, notional **$2.00** → at cap; no clip needed.

If **`max_notional_policy: deny`** instead:

- `qty = 8`, notional **$3.20** → **risk_denied** (`RISK_ORDER_DEPLOYMENT_EXCEEDED`).

So **the same guru trade** lands in **different outcomes** depending on **scale**, **caps**, and **per-order policies** — that’s what you’re tuning.

---

## 5. Short “decision tree”

```
Guru trade happens
  → Signal created?
       NO  → silence (no guru activity or wrong wallet)
       YES → Entry policy OK?
              NO  → copy_skip (e.g. token filter)
              YES → qty > 0?
                     NO  → copy_skip (zero_qty)
                     YES → risk.evaluate OK?  (min/max deploy, clip/bump, caps, …)
                            NO  → risk_denied (stay flat)
                            YES → shadow: log only | live: submit_order
```

---

## 6. What you should remember

1. **Nothing after “websocket subscribed”** can still be normal if **the guru doesn’t trade**; the pipeline only runs when a **`GuruTradeSignal`** is produced for **your** guru wallet.
2. **Strategy** = *whether* to copy and *how big* (scale, optional conviction).
3. **Risk** = *whether your rules allow the intent*, including per-order min/max deploy (**deny** vs **clip**/**bump**), token/portfolio caps, and capital gates.
4. **Execution** = *actually placing* the order (live) or not (shadow).

5. **Deployment caps** (`portfolio_deploy`, token caps, pending limits) come from **Nautilus framework state** (open orders + open positions), **not** from wallet cash — USDC/allowance checks are a separate **capital gate**. **One bot, one wallet** is the supported live model; manual UI or other actors on the same wallet can desync framework truth until reconciliation catches up. Read **[OPERATIONS.md](OPERATIONS.md)** § *Current status & operating model* and the validation index in **[README.md](README.md)**.

If you want, we can take **one line from your `facts.jsonl`** (e.g. one `sizing` + one `risk_decision`) and walk through that exact trade with your real parameters step by step.