# R7A.1 — Market-time, position-scope, fee, and readiness correction

**Base:** R6D / R7A at `f77c5c1` (uncommitted R7A+R7A.1 working tree)  
**Mutations:** none  
**Approval artifact:** not issued  
**Flatten / redeem / submit / cancel:** not performed  

Artifacts (gitignored): `var/reporting/r7/r7a_report.json`, `r7a1_position_inventory.json`

---

## 1. Market-time correction

### Root cause

R7A set `event_start` / `event_end` from Gamma **`event.startDate`** / **`event.endDate`**.

For BTC 5m markets:

| Field | Example value | Meaning |
|-------|---------------|---------|
| `event.startDate` / `market.startDate` | `2026-07-16T06:22Z` | **Listing / publication** (~1 day early) |
| `event.createdAt` / `market.createdAt` | ~same day as listing | Object creation |
| `event.startTime` / `market.eventStartTime` | `2026-07-17T06:15:00Z` | Trading window start |
| `market.endDate` / `event.endDate` | `2026-07-17T06:20:00Z` | Trading window end |
| Slug `btc-updown-5m-{epoch}` | epoch = unix start | **Authoritative start** |

Using `startDate` as `market_start` produced a ~one-day interval that disagreed with the title (`2:10–2:15 AM ET`).

### Field provenance

| Internal field | API field/source | Semantic meaning | Trusted for scheduling? |
| -------------- | ---------------- | ---------------- | ----------------------- |
| `market_start` | slug epoch (+ cross-check `eventStartTime` / `startTime`) | UTC start of 5m window | **Yes** |
| `market_end` | `market_start + 300s` (+ cross-check `endDate`) | UTC end of 5m window | **Yes** |
| `created_at` | `createdAt` | Gamma creation | No |
| `listed_at` | `startDate` | Listing/publication | No |
| `slug_epoch` | slug suffix | Unix seconds of start | **Yes** |

### Correct window (example from live probe)

```text
slug:    btc-updown-5m-1784269200
start:   2026-07-17T06:20:00Z   (= title 2:20AM ET)
end:     2026-07-17T06:25:00Z   (= start + 300s)
listed:  2026-07-16T06:27:32Z   (not used for scheduling)
```

### Deadline invariant (no more `now+10m`)

```text
approval_expiration ≤ entry_deadline < flatten_deadline < market_end

flatten_deadline = market_end − 30s
entry_deadline   = flatten_deadline − 45s
approval_expiration = entry_deadline − 5s
```

Example for the 06:20–06:25 window:

| Deadline | UTC |
|----------|-----|
| flatten | 06:24:30 |
| entry | 06:23:45 |
| approval expiry | 06:23:40 |

Typed blockers: `INVALID_MARKET_WINDOW`, `MARKET_DURATION_MISMATCH`, `TITLE_TIME_MISMATCH`, `ENTRY_DEADLINE_AFTER_FLATTEN`, `INSUFFICIENT_TIME_REMAINING`, `MARKET_NOT_ACCEPTING_ORDERS`.

Module: `src/tyrex_pm/adapters/polymarket/btc_5m_window.py`

---

## 2. Position inventory (read-only)

All four nonzero Data API positions:

| # | Title (short) | Qty | curPrice | redeemable | Category | Selected? | Likely Tyrex? | Action |
|---|---------------|-----|----------|------------|----------|-----------|---------------|--------|
| 1 | LoL BLG vs HLE | 13.51 | 0 | true | `RESOLVED_REDEEMABLE_POSITION` | No | No | Redeem auth only |
| 2 | BTC 5m Jun 29 1:20–1:25PM ET | 9.26 | 0 | true | `RESOLVED_REDEEMABLE_POSITION` | No | Yes (~$5 loss) | Redeem auth only |
| 3 | BTC 5m Jul 2 3:50–3:55AM ET | 5 | 0 | true | `RESOLVED_REDEEMABLE_POSITION` | No | Yes | Redeem auth only |
| 4 | BTC 5m Jul 2 3:10–3:15AM ET | 5 | 0 | true | `RESOLVED_REDEEMABLE_POSITION` | No | Yes | Redeem auth only |

**None** match the proposed R7 token/market. **None** are CLOB-tradable (`curPrice=0`, `redeemable=true`). Do **not** CLOB-sell them. Redemption requires separate on-chain authorization. No cleanup performed.

---

## 3. Reconciliation policy

### Distinction

```text
Reconciliation clean: Local and venue truth agree.
Position flat:        The agreed quantity is zero.
```

A nonzero resolved position can be cleanly reconciled and still present account exposure.

### Selected-market (mandatory for R7B)

- No position on selected YES/NO  
- No working order / unresolved fill / unknown submission for that market  
- Local ↔ venue agreement on that market  

### Account-wide

- Unrelated exposure inventoried  
- No unknown external working orders  
- Balance not undermined  
- New BUY budget independently capped at $5  

### Recommendation (explicit — not silently chosen)

| Option | Name | Verdict |
|--------|------|---------|
| 1 | Require globally flat | Safe; redemption of losers is a separate project |
| 2 | Permit known `RESOLVED_REDEEMABLE` with explicit ack | Acceptable if selected-market flat |
| 3 | Dedicated clean account | **Preferred cleanliness** for first live validation |

**Recommendation:** Prefer **(3)** dedicated clean account for R7B. Acceptable alternative **(2)** with explicit user acknowledgment of the four redeemable rows. Do **not** create/fund a wallet without authorization. Do **not** treat these as CLOB flatten targets.

Current authoritative blockers still include `ACCOUNT_EXPOSURE_PRESENT` under policy `require_ack_resolved_redeemable` until the user explicitly chooses and acknowledges.

---

## 4. Readiness (unified)

Authoritative object: `r7_readiness` (consumed by CLI + report).  
R6D observe-gate readiness remains separate for auth validation and must not hide R7 blockers.

This run:

```text
ready_for_r7b_proposal: false
mutations_enabled: false
blockers:
  - MUTATIONS_DISABLED
  - R7B_AUTHORIZATION_ABSENT
  - ACCOUNT_EXPOSURE_PRESENT
```

`MUTATIONS_DISABLED` no longer appears alone when exposure/time/fee blockers exist.

---

## 5. Fees

| Item | Value |
|------|-------|
| Source | `GET /clob-markets/{condition_id}` → `fd` |
| Docs | https://docs.polymarket.com/trading/fees |
| Crypto sample | `fd = { r: 0.07, e: 1, to: true }` |
| Formula | `fee_usdc = shares × r × (p×(1−p))**e` |
| FAK BUY `amount` | USDC spent on shares ([SDK](https://docs.polymarket.com/trading/orders/create)) |
| Fee vs amount | Fee treated as **additional** USDC debit |
| Cap rule | `amount + max_buy_fee ≤ $5.00` |
| This probe | amount **4.83**, fee **~0.166**, collateral **~4.996** |

If `fd` missing → `FEE_PARAMETERS_UNKNOWN` blocks.

Module: `src/tyrex_pm/execution/polymarket/fees_fd.py`

---

## 6. FAK sizing semantics

- BUY `amount` = USDC dollars (not guaranteed shares)  
- `price` = worst-price limit  
- Report field renamed to **`max_estimated_shares`** (not expected/guaranteed)  
- Partial fill + residual cancel are venue FAK behavior; residual cancel may be unnecessary if fully killed  

---

## 7. Exit policy (exact)

| Item | Policy |
|------|--------|
| Normal exit | FAK SELL |
| Quantity | Venue-confirmed position only |
| Min sell price | Start at best bid; escalate −1/−2 ticks; emergency floor = tick_size |
| Max attempts / interval | 3 / 2s |
| Flatten deadline | Emergency worst-price FAK once |
| Empty/one-sided bid book | `MANUAL_INTERVENTION` |
| Max exit fee | `fd` at exit price × shares |
| Worst-case loss | May approach full acquisition cost (amount + entry fee), up to the $5 collateral envelope — **not** “low residual risk” |

---

## 8. Safety

| Check | Result |
|-------|--------|
| Tests | **252** passed (R7A.1 adds market-window, inventory, readiness, fee suites) |
| Real mutations | **Zero** |
| Approval artifact | **None** |
| Position cleanup | **None** |
| `.env` SHA256 | `27210C97AE37101DE48570130BBB517E572F3EB75160B5FC4C05CF178B91F772` (unchanged) |
| `live-once` | Still refuses |
| Heartbeat | Not called |

---

## Stop

R7A.1 complete. Do not flatten, redeem, submit, cancel, approve, or begin R7B until the user chooses an account policy and issues a fresh authorization against a new artifact.
