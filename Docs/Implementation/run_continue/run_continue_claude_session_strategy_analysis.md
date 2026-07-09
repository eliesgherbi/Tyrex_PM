# run_continue Claude Session Strategy Analysis

**Session analyzed:** `claude_trailing_continue_smoke` (session_id `a3fc055b-74db-4582-99fb-477874fb348e`)  
**Started:** 2026-07-08T14:59:32 UTC (≈ 10:59 AM ET)  
**Scope:** 8 completed DONE windows (`1505`–`1540` UTC = 11:00–11:40 AM ET)  
**Scenario:** `live_paired_binary_claude_trailing_enforce.yaml`  
**Git SHA:** `8f8ef51d0db25f53b5eddf1f509ccc61c4f9b9eb`

---

## 1. Executive Summary

This was the **first successful multi-window `run_continue` session** on the Claude-tuned survivor scenario after resetting kill-switch state. All **8 windows completed** (`exit_code=0`, `final_state=DONE`) with full pair entry → dual-leg stop → survivor enforce exit.

| Metric | Value |
|--------|-------|
| Windows completed | **8 / 8** |
| Pair entries | **8** (100%) at pair cost **1.01** |
| Bot tentative net PnL | **−$1.90** (−$0.24/pair avg) |
| Bot winners / losers | **3 / 5** (by tentative `pnl_total > 0`) |
| Polymarket UI net (8 windows) | **−$5.43** |
| Reconciliation gap (UI − bot) | **−$3.53** |

**Dominant exit path:** loser-leg **pair stop** (12% budget) → survivor **trailing_stop** `trail_floor_breach` (7/8 windows). One window (**1530**) used **`survivor_floor` hard_floor_breach** with the new `floor_buffer: -0.08` geometry (floor = entry − 8¢).

**vs. phase1 session (2026-07-07):** Phase1 had 3 winners that **held survivor to resolution** without enforce exit. This Claude session had 3 tentative winners but **all exited via survivor enforce** (trailing or floor)—none held to resolution. Tighter `trail_distance: 0.10` and enforce mode cut winners earlier when price retraced.

**PnL gap vs. Polymarket UI:** On **7 complete windows**, bot reports systematically **~$0.34–0.35 more favorable** per window than UI cash flows. Window **1540** accounts for **$2.14** of the total gap because the UI snapshot shows **entry only** (no sells yet) while the bot recorded a full round-trip (−$0.50).

---

## 2. Scope and Evidence Reviewed

### Session artifacts

| Path | Used |
|------|------|
| `var/reporting/run_continue/claude_trailing_continue_smoke/session_manifest.json` | Yes |
| `var/reporting/run_continue/claude_trailing_continue_smoke/session_log.jsonl` | Yes |

### Per-window run directories

| UTC window | ET window | Run directory |
|------------|-----------|---------------|
| 1505 | 11:00–11:05 | `claude_trailing_continue_smoke__btc_5m_20260708_1505/` |
| 1510 | 11:05–11:10 | `claude_trailing_continue_smoke__btc_5m_20260708_1510/` |
| 1515 | 11:10–11:15 | `claude_trailing_continue_smoke__btc_5m_20260708_1515/` |
| 1520 | 11:15–11:20 | `claude_trailing_continue_smoke__btc_5m_20260708_1520/` |
| 1525 | 11:20–11:25 | `claude_trailing_continue_smoke__btc_5m_20260708_1525/` |
| 1530 | 11:25–11:30 | `claude_trailing_continue_smoke__btc_5m_20260708_1530/` |
| 1535 | 11:30–11:35 | `claude_trailing_continue_smoke__btc_5m_20260708_1535/` |
| 1540 | 11:35–11:40 | `claude_trailing_continue_smoke__btc_5m_20260708_1540/` |

### External evidence

Polymarket UI activity history (user-provided, July 8 ET labels). Amounts summed per 5-minute window (4 legs: buy Up, buy Down, sell Up, sell Down).

### Excluded

- Failed smoke runs earlier on 2026-07-08 (1415–1450) — entry-layer failures, not strategy diagnosis.
- Window `1545` — scheduled in `session_log.jsonl` but no `window_run_start` recorded (session ended or interrupted after 1540).

---

## 3. Effective Configuration (Claude scenario)

Effective config = `paired_binary.yaml` + `live_paired_binary_claude_trailing_enforce.yaml`.

**Note:** For this successful run, `max_pair_entry_cost` was **`1.015`** in the scenario file (not `1.000`). All entries occurred at **pair cost 1.01**, consistent with the 1.015 cap.

### Entry / stop (changed vs phase1)

| Parameter | Claude value | Phase1 value | Session effect |
|-----------|--------------|--------------|----------------|
| `max_pair_entry_cost` | **1.015** | 1.015 | All 8 entries at **1.01** |
| `pair_stop_loss_pct` | **0.12** | 0.09 | `loss_budget = 0.1212` per pair |
| `slippage_buffer` | 0.005 | 0.005 | Wider stop trigger vs 9% run |

### Survivor (Claude tuning)

| Parameter | Claude value | Phase1 value | Session effect |
|-----------|--------------|--------------|----------------|
| `survivor_floor.floor_buffer` | **−0.08** | 0.00 | Floor = entry − 8¢ (e.g. NO@0.50 → **0.42**) |
| `trailing_stop.trail_distance` | **0.10** | 0.20 | Tighter trail → more `trail_floor_breach` |
| `trailing_stop.min_profit_lock` | **0.00** | 0.01 | No 1¢ lock above entry on trail |
| `trailing_stop.recovery_buffer` | 0.00 | 0.00 | Arms at breakeven (not `activation_buffer`) |
| `recovery_level.activation_buffer` | 0.04 | 0.00 | **Not wired** to trailing arm threshold |

**Stop trigger example** (pair_cost 1.01, YES entry 0.51):

```
loss_budget = 1.01 × 0.12 = 0.1212
YES planned_stop = 0.51 − 0.1212 = 0.3888 → trigger ≈ 0.394
NO  planned_stop = 0.50 − 0.1212 = 0.3788 → trigger ≈ 0.384
```

**Hard floor example** (NO survivor entry 0.50):

```
floor_price = 0.50 + (−0.08) = 0.42   # observed in window 1530
```

---

## 4. Session Timeline

| # | UTC | ET | market_id | Duration | Outcome | Tentative PnL |
|---|-----|-----|-----------|----------|---------|---------------|
| 1 | 14:59–15:01 | 11:00–11:05 | 1505 | ~108s | DONE | −$1.00 |
| 2 | 15:04–15:06 | 11:05–11:10 | 1510 | ~153s | DONE | +$0.05 |
| 3 | 15:09–15:12 | 11:10–11:15 | 1515 | ~230s | DONE | +$0.85 |
| 4 | 15:14–15:15 | 11:15–11:20 | 1520 | ~97s | DONE | −$0.35 |
| 5 | 15:19–15:20 | 11:20–11:25 | 1525 | ~116s | DONE | −$0.30 |
| 6 | 15:24–15:26 | 11:25–11:30 | 1530 | ~140s | DONE | −$0.90 |
| 7 | 15:29–15:31 | 11:30–11:35 | 1535 | ~152s | DONE | +$0.25 |
| 8 | 15:34–15:36 | 11:35–11:40 | 1540 | ~142s | DONE | −$0.50 |
| | | | **Total** | | **8/8** | **−$1.90** |

Every window: `paired_binary_pair_entry_committed` → `paired_binary_activation_reference` → `paired_binary_stop_plan` → `survival_enforce_exit_submitted` → `paired_binary_realized_pnl_tentative`.

---

## 5. Per-Window Summary

Legend: prices from `paired_binary_activation_reference` and `paired_binary_realized_pnl_tentative`. UI amounts from Polymarket activity feed.

### 5.1 Window 1505 (11:00–11:05 ET) — loser

| Field | Bot | UI |
|-------|-----|-----|
| Entry YES / NO | 0.50 / 0.51 | 51.7¢ / 52.7¢ |
| Stop leg / survivor | **NO** / **YES** | — |
| Loser exit / survivor exit | NO ~0.41 / YES ~0.40 | 39.3¢ / 38.3¢ |
| Survivor exit reason | `trailing_stop:trail_floor_breach` | — |
| PnL | **−$1.00** | **−$1.34** |
| Gap (bot − UI) | | **+$0.34** |

**Interpretation:** Standard flow. YES survivor peaked then retraced; trailing at 0.10 distance forced exit. UI shows slightly worse fills on both exits.

---

### 5.2 Window 1510 (11:05–11:10 ET) — marginal winner

| Field | Bot | UI |
|-------|-----|-----|
| Entry | 0.51 / 0.50 | 52.7¢ / 51.7¢ |
| Stop leg / survivor | **NO** / **YES** | — |
| Exits | NO 0.36 / YES **0.66** | 34.4¢ / **64.4¢** |
| Exit reason | `trail_floor_breach` | — |
| PnL | **+$0.05** | **−$0.29** |
| Gap | | **+$0.34** |

**Interpretation:** YES survivor ran to ~0.66+; trailing locked profit but bot barely net positive. UI shows net loss despite similar exit prices — **entry cash drag** (UI buys −$5.23 vs bot $5.05) explains most of gap.

---

### 5.3 Window 1515 (11:10–11:15 ET) — best winner ✓

| Field | Bot | UI |
|-------|-----|-----|
| Entry | 0.51 / 0.50 | 52.7¢ / 51.7¢ |
| Stop leg / survivor | **YES** / **NO** | — |
| Exits | YES 0.40 / NO **0.78** | 38.3¢ / **76.8¢** |
| Exit reason | `trail_floor_breach` on NO | — |
| PnL | **+$0.85** | **+$0.53** |
| Gap | | **+$0.32** |

**Interpretation:** Clear directional window (NO strengthened). YES stopped early; NO survivor rode move then trailing exit at 0.78. Bot more optimistic than UI on NO exit (~0.78 vs 76.8¢) and overall PnL.

---

### 5.4 Window 1520 (11:15–11:20 ET) — loser

| Field | Bot | UI |
|-------|-----|-----|
| Entry | 0.54 / 0.47 | 55.7¢ / 48.7¢ |
| Stop leg / survivor | **NO** / **YES** | — |
| Exits | NO 0.37 / YES 0.57 | 35.4¢ / 55.3¢ |
| PnL | **−$0.35** | **−$0.70** |
| Gap | | **+$0.35** |

---

### 5.5 Window 1525 (11:20–11:25 ET) — loser

| Field | Bot | UI |
|-------|-----|-----|
| Entry | 0.53 / 0.48 | 54.7¢ / 49.7¢ |
| Stop leg / survivor | **YES** / **NO** | — |
| Exits | YES 0.35 / NO 0.60 | 33.4¢ / 58.3¢ |
| PnL | **−$0.30** | **−$0.64** |
| Gap | | **+$0.34** |

---

### 5.6 Window 1530 (11:25–11:30 ET) — loser (hard floor)

| Field | Bot | UI |
|-------|-----|-----|
| Entry | 0.51 / 0.50 | 52.7¢ / 51.7¢ |
| Stop leg / survivor | **YES** / **NO** | — |
| Survivor module | **`survivor_floor`** `hard_floor_breach` | — |
| Floor price | **0.42** (0.50 − 0.08) | — |
| Exits | YES 0.37 / NO 0.46 | 35.4¢ / 44.3¢ |
| PnL | **−$0.90** | **−$1.25** |
| Gap | | **+$0.35** |

**Interpretation:** Only window where **`survivor_floor` dispatched** instead of trailing. Negative `floor_buffer` allowed NO to trade down to 0.42 before forced exit — still lost as YES stop + NO floor exit combined poorly.

---

### 5.7 Window 1535 (11:30–11:35 ET) — marginal winner ✓

| Field | Bot | UI |
|-------|-----|-----|
| Entry | 0.54 / 0.47 | 55.7¢ / 48.7¢ |
| Stop leg / survivor | **YES** / **NO** | — |
| Exits | YES 0.42 / NO **0.64** | 40.3¢ / **62.4¢** |
| PnL | **+$0.25** | **−$0.10** |
| Gap | | **+$0.35** |

**Interpretation:** Bot winner, UI small loser — largest **directional** disagreement besides 1540. NO survivor exit 0.64 vs UI 62.4¢; YES stop 0.42 vs UI 40.3¢ align reasonably.

---

### 5.8 Window 1540 (11:35–11:40 ET) — loser (UI incomplete)

| Field | Bot | UI |
|-------|-----|-----|
| Entry | 0.51 / 0.50 | 52.7¢ / *(no Down buy in UI feed)* |
| Stop leg / survivor | **NO** / **YES** | — |
| Exits | NO 0.37 / YES 0.54 | **Not shown in UI** |
| PnL | **−$0.50** | **−$2.64** (entry only) |
| Gap | | **+$2.14** |

**Interpretation:** Bot completed full lifecycle. UI snapshot captured **Achat Up 52.7¢ −$2.64 only** — sells not yet in feed when screenshot taken. **Do not use 1540 UI PnL for session totals** until sells appear.

---

## 6. Stop-Leg Selection

| Window | YES entry | NO entry | Loser leg | Survivor | Loser bid @ stop (approx) |
|--------|-----------|----------|-----------|----------|---------------------------|
| 1505 | 0.50 | 0.51 | NO | YES | ≤ trigger |
| 1510 | 0.51 | 0.50 | NO | YES | ≤ trigger |
| 1515 | 0.51 | 0.50 | YES | NO | 0.37 |
| 1520 | 0.54 | 0.47 | NO | YES | ≤ trigger |
| 1525 | 0.53 | 0.48 | YES | NO | ≤ trigger |
| 1530 | 0.51 | 0.50 | YES | NO | 0.37 |
| 1535 | 0.54 | 0.47 | YES | NO | ≤ trigger |
| 1540 | 0.51 | 0.50 | NO | YES | ≤ trigger |

**YES stopped 4× / NO stopped 4×** — balanced, consistent with `evaluate_dual_stop()` firing on whichever leg hits 12% trigger first. No evidence of wrong-leg selection bias.

---

## 7. Survivor Exit Analysis

| Window | Module | Reason | Survivor leg | Exit price (survivor) | Tentative PnL |
|--------|--------|--------|--------------|----------------------|---------------|
| 1505 | trailing_stop | trail_floor_breach | YES | 0.40 | −$1.00 |
| 1510 | trailing_stop | trail_floor_breach | YES | 0.66 | +$0.05 |
| 1515 | trailing_stop | trail_floor_breach | NO | 0.78 | +$0.85 |
| 1520 | trailing_stop | trail_floor_breach | YES | 0.57 | −$0.35 |
| 1525 | trailing_stop | trail_floor_breach | NO | 0.60 | −$0.30 |
| 1530 | **survivor_floor** | **hard_floor_breach** | NO | 0.46 | −$0.90 |
| 1535 | trailing_stop | trail_floor_breach | NO | 0.64 | +$0.25 |
| 1540 | trailing_stop | trail_floor_breach | YES | 0.54 | −$0.50 |

**Observations:**

1. **7/8 exits = trailing `trail_floor_breach`** with `trail_distance=0.10`. Retracements of 10¢ from peak routinely force exit.
2. **`min_profit_lock=0`** did not prevent exits near entry when peak − 0.10 ≤ entry (tight trail dominates).
3. **`floor_buffer=-0.08`** fired once (1530) when bid breached 0.42 on NO — confirms negative buffer is live in enforce path.
4. **No resolution holds** — unlike phase1 winners (1750, 1805, 1815), no survivor rode to $0.95+ resolution exit.
5. **Quality reject retries** on survivor submit were common (`reconnect_gap` / `sequence_gap`); retries succeeded within ~1s (1510, 1515, 1525).

---

## 8. Polymarket UI vs Bot PnL Reconciliation

### 8.1 Methodology

- **UI PnL:** Sum of 4 activity lines per ET window (buy/sell Up/Down, ±$ amounts from Polymarket feed).
- **Bot PnL:** `paired_binary_realized_pnl_tentative.pnl_total` per window.
- **Mapping:** UTC `HHMM` window start = ET `(HH−4):MM` for July EDT (e.g. 1505 UTC → 11:00–11:05 ET).

### 8.2 Reconciliation table

| Window | ET | Bot PnL | UI PnL | Δ (bot − UI) | Notes |
|--------|-----|---------|--------|--------------|-------|
| 1505 | 11:00–11:05 | −$1.00 | −$1.34 | +$0.34 | Aligns |
| 1510 | 11:05–11:10 | +$0.05 | −$0.29 | +$0.34 | UI worse despite similar sells |
| 1515 | 11:10–11:15 | +$0.85 | +$0.53 | +$0.32 | Bot higher on NO exit |
| 1520 | 11:15–11:20 | −$0.35 | −$0.70 | +$0.35 | |
| 1525 | 11:20–11:25 | −$0.30 | −$0.64 | +$0.34 | |
| 1530 | 11:25–11:30 | −$0.90 | −$1.25 | +$0.35 | |
| 1535 | 11:30–11:35 | +$0.25 | −$0.10 | +$0.35 | Sign flip |
| 1540 | 11:35–11:40 | −$0.50 | −$2.64* | +$2.14 | *UI entry-only |
| **Total** | | **−$1.90** | **−$5.43** | **+$3.53** | |

### 8.3 Gap drivers

| Driver | Evidence | Estimated impact |
|--------|----------|------------------|
| **Tentative exit cashflows** | All windows: `manual_reconciliation_required: true`; mix of `venue_reconciled` entries + `ws_trade` exits | ~$0.34/window systematic |
| **Entry price display** | UI shows 51.7–55.7¢; bot facts 50–54¢ (rounded match evidence) | Cents-level per leg |
| **1540 incomplete UI** | UI missing sell legs; bot has full round-trip | **$2.14** of total gap |
| **Fees / rebates** | Not modeled in bot `pnl_total` | Unknown; likely small |
| **FAK reprice on survivor exit** | `survival_exit_order_repriced` with lower prices on retries (1505, 1540) | Bot may understate slippage on ws_trade path |

**Adjusted estimate (exclude 1540 UI anomaly):**

```
UI total (7 complete windows)  = −$5.43 − (−$2.64) = −$2.79  (remove entry-only 1540)
Bot total (7 windows)          = −$1.90 − (−$0.50) = −$1.40
Gap (7 windows)                ≈ −$1.39  (~$0.20/window UI worse)
```

Bot is still more favorable than UI on complete windows, but gap shrinks from $3.53 → ~$1.39 when 1540 UI is excluded.

### 8.4 Price alignment (exit legs)

| Window | UI sell Up | Bot YES exit | UI sell Down | Bot NO exit |
|--------|------------|--------------|--------------|-------------|
| 1505 | 38.3¢ | 40.0¢ | 39.3¢ | 41.0¢ |
| 1510 | 64.4¢ | 66.0¢ | 34.4¢ | 36.0¢ |
| 1515 | 38.3¢ | 40.0¢ | 76.8¢ | 78.0¢ |
| 1520 | 55.3¢ | 57.0¢ | 35.4¢ | 37.0¢ |
| 1525 | 33.4¢ | 35.0¢ | 58.3¢ | 60.0¢ |
| 1530 | 35.4¢ | 37.0¢ | 44.3¢ | 46.0¢ |
| 1535 | 40.3¢ | 42.0¢ | 62.4¢ | 64.0¢ |
| 1540 | — | 54.0¢ | — | 37.0¢ |

Exit prices are **close but not identical** — bot often **1–2¢ higher** on survivor exits (ws_trade timing) and **1–2¢ lower** on stop legs. Net effect in this sample: bot PnL **slightly optimistic** vs wallet.

---

## 9. Claude Params vs Phase1 — Outcome Comparison

| Dimension | Phase1 (17:30 session) | Claude (this session) |
|-----------|------------------------|------------------------|
| Net tentative PnL | −$1.65 / 13 windows | −$1.90 / 8 windows |
| Win rate | 3/13 (23%) | 3/8 (38%) |
| Resolution holds | 3 winners | **0** |
| Dominant survivor exit | floor + tight min_lock trail | **trail_distance 0.10** |
| Stop budget | 9% | **12%** (wider — fewer false stops?) |
| Entry cost cap | 1.015 | 1.015 (this run) |
| Hard floor | entry + 0 | **entry − 0.08** (1 enforce) |

**Hypothesis:** Wider stop (12%) + tighter trail (10¢) shifts PnL from “stop whipsaw” to “survivor trail cuts winners before resolution.” Session win rate improved but **no big resolution winner** like phase1 +$1.40 (1750).

---

## 10. Key Failure Modes Observed

1. **Survivor trailing cuts recoveries** — 7/8 windows exited on `trail_floor_breach` with 10¢ trail; several survivors had been profitable (1510 YES@0.66, 1515 NO@0.78) but net pair PnL still marginal or negative after stop leg + 1.01 entry drag.

2. **Structural pair premium 1.01** — Every entry at 1.01 imposes ~$0.05/pair headwind vs $1.00 fair pair.

3. **Tentative PnL optimism** — Systematic ~$0.34/window gap vs UI on complete windows; `ws_trade` survivor exits vs `venue_reconciled` stops.

4. **WS quality on survivor dispatch** — Frequent `sequence_gap` / `reconnect_gap` quality rejects before retry; adds latency but retries succeeded this session.

5. **No entry-layer failures** — Contrast with earlier smoke runs (FAK no-match, kill-switch, `max_pair_entry_cost` blocks). This session had clean entry after config/state fixes.

---

## 11. Questions Before Next Tuning Iteration

1. Is **`trail_distance: 0.10`** intentionally tighter than phase1 0.20 for 5m BTC, knowing it prevents resolution holds?
2. Should **`floor_buffer: -0.08`** be paired with **higher `trail_distance`** to avoid double-tight geometry (floor at −8¢ AND trail at −10¢ from peak)?
3. Wire **`trailing_stop.recovery_buffer: 0.04`** to match `activation_buffer` intent before next live run?
4. Require **venue-reconciled exit cashflows** before trusting PnL for parameter decisions?
5. Re-run **`scripts/reconcile_run_cashflows.py`** per window to replace tentative totals?

---

## 12. Recommendations (analysis only — no implementation)

| Priority | Action |
|----------|--------|
| P0 | Reconcile 8 windows with venue cashflows; confirm −$1.90 bot total |
| P1 | Advisory-replay Claude params on recorded windows — compare trail_distance 0.10 vs 0.15 vs 0.20 |
| P1 | If resolution holds desired, test **wider trail** or **later arm** (`recovery_buffer: 0.04`) |
| P2 | Keep `max_pair_entry_cost: 1.015` for smoke until entry reliability proven at 1.000 |
| P2 | Phase 2 `run_continue --continue-on-window-failure` for operational continuity (not strategy) |

---

## Appendix A: UI Activity Parse (raw)

| ET window | Buys | Sells | UI net |
|-----------|------|-------|--------|
| 11:00–11:05 | Up 52.7¢ −$2.64, Down 51.7¢ −$2.59 | Up 38.3¢ +$1.92, Down 39.3¢ +$1.97 | −$1.34 |
| 11:05–11:10 | Up 52.7¢ −$2.64, Down 51.7¢ −$2.59 | Up 64.4¢ +$3.22, Down 34.4¢ +$1.72 | −$0.29 |
| 11:10–11:15 | Up 52.7¢ −$2.64, Down 51.7¢ −$2.59 | Up 38.3¢ +$1.92, Down 76.8¢ +$3.84 | +$0.53 |
| 11:15–11:20 | Up 55.7¢ −$2.79, Down 48.7¢ −$2.44 | Up 55.3¢ +$2.76, Down 35.4¢ +$1.77 | −$0.70 |
| 11:20–11:25 | Up 54.7¢ −$2.74, Down 49.7¢ −$2.49 | Up 33.4¢ +$1.67, Down 58.3¢ +$2.92 | −$0.64 |
| 11:25–11:30 | Up 52.7¢ −$2.64, Down 51.7¢ −$2.59 | Up 35.4¢ +$1.77, Down 44.3¢ +$2.21 | −$1.25 |
| 11:30–11:35 | Up 55.7¢ −$2.79, Down 48.7¢ −$2.44 | Up 40.3¢ +$2.01, Down 62.4¢ +$3.12 | −$0.10 |
| 11:35–11:40 | Up 52.7¢ −$2.64 only | — | −$2.64* |

---

## Appendix B: Comparison to Prior Analysis

Reference: [run_continue_last_session_strategy_analysis.md](./run_continue_last_session_strategy_analysis.md) (phase1_trailing_continue_smoke, 2026-07-07).

| Metric | Phase1 session | Claude session |
|--------|----------------|----------------|
| Windows | 13 DONE + 1 interrupted | 8 DONE |
| Scenario | `live_paired_binary_phase1_trailing_enforce` | `live_paired_binary_claude_trailing_enforce` |
| Net tentative PnL | −$1.65 | −$1.90 |
| UI reconciliation | Not available | −$5.43 UI vs −$1.90 bot |
| Primary loss driver | floor@entry + min_lock trail | **trail_distance 0.10** enforce |
| Biggest winner | +$1.40 (resolution hold) | +$0.85 (trail exit NO@0.78) |

---

*Report generated from live run artifacts + user-provided Polymarket UI activity. All bot PnL figures are `tentative` per facts; venue reconciliation recommended before parameter changes.*
