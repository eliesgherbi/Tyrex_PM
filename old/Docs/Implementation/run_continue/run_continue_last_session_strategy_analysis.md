# run_continue Last Session Strategy Analysis

**Session analyzed:** `phase1_trailing_continue_smoke` (session_id `51502182-72a0-416d-b895-e5d1ab8aad18`)  
**Started:** 2026-07-07T17:30:04 UTC  
**Scope:** 13 completed windows (`1740`–`1840`) + interrupted window `1845`  
**Git SHA (session manifest):** `8f8ef51d0db25f53b5eddf1f509ccc61c4f9b9eb`

---

## 1. Executive Summary

The 17:30 UTC `run_continue` session operated correctly end-to-end: pair entry → activation → dual-leg stop → survivor protection → window completion, chaining **13 consecutive DONE windows** with net **tentative PnL −$1.65** (3 winners / 10 losers).

Losses were **not primarily caused by wrong loser-leg selection**. Stop-leg logic fired when a leg’s bid crossed its configured **9% pair stop trigger** (`pair_stop_loss_pct=0.09`, `slippage_buffer=0.005`), and selection was mechanically consistent with `evaluate_dual_stop()` in `exit_engine.py`. YES was stopped more often (9/13) because **only that leg’s trigger was hit** in those windows, not because of asymmetric parameters.

Losses were **dominated by survivor exit policy**, especially:

1. **`survivor_floor` with `mode: winner_entry_price` and `floor_buffer: 0.00`** — hard floor equals survivor entry; any executable bid at or below entry forces exit (`survivor_floor.py`).
2. **`trailing_stop.min_profit_lock: 0.01` combined with `trail_distance: 0.20`** — until peak exceeds ~entry + trail_distance + min_lock, the effective trail floor sits at **entry + $0.01**, only 1¢ above the hard floor. Retracements after partial recovery routinely trigger `trail_floor_breach`.
3. **Pair entry cost ~1.01** — structural ~$0.05/pair drag vs perfect $1.00 pair; `desired_net_profit_per_pair` was **$0.2121** but realized survivor exits often near entry/below breakeven (~$0.60+ recovery level).

The **3 winners** (`1750`, `1805`, `1815`) shared one pattern: **NO survivor held through resolution** (final NO bids **0.87–0.99**) with **no `survival_enforce_exit_submitted`**. Trailing armed but did not force exit before lifecycle completion.

**Primary tuning question:** whether `floor_buffer=0` + `min_profit_lock=0.01` is intentionally tight for 5-minute BTC binaries, or whether survivor protection should allow more room after pair-cost drag and loser-leg slippage.

---

## 2. Scope and Evidence Reviewed

### Session artifacts

| Path | Used |
|------|------|
| `var/reporting/run_continue/phase1_trailing_continue_smoke/session_manifest.json` | Yes |
| `var/reporting/run_continue/phase1_trailing_continue_smoke/session_log.jsonl` | Yes |

### Per-window run directories (all under `var/reporting/runs/`)

| Window | Run directory |
|--------|---------------|
| 1740 | `phase1_trailing_continue_smoke__btc_5m_20260707_1740/` |
| 1745 | `phase1_trailing_continue_smoke__btc_5m_20260707_1745/` |
| 1750 | `phase1_trailing_continue_smoke__btc_5m_20260707_1750/` |
| 1755 | `phase1_trailing_continue_smoke__btc_5m_20260707_1755/` |
| 1800 | `phase1_trailing_continue_smoke__btc_5m_20260707_1800/` |
| 1805 | `phase1_trailing_continue_smoke__btc_5m_20260707_1805/` |
| 1810 | `phase1_trailing_continue_smoke__btc_5m_20260707_1810/` |
| 1815 | `phase1_trailing_continue_smoke__btc_5m_20260707_1815/` |
| 1820 | `phase1_trailing_continue_smoke__btc_5m_20260707_1820/` |
| 1825 | `phase1_trailing_continue_smoke__btc_5m_20260707_1825/` |
| 1830 | `phase1_trailing_continue_smoke__btc_5m_20260707_1830/` |
| 1835 | `phase1_trailing_continue_smoke__btc_5m_20260707_1835/` |
| 1840 | `phase1_trailing_continue_smoke__btc_5m_20260707_1840/` |
| 1845 | `phase1_trailing_continue_smoke__btc_5m_20260707_1845/` |

### Per-window files inspected

| File | Status |
|------|--------|
| `manifest.json` | Present (run_id, execution_mode, run_name) |
| `facts.jsonl` | Primary evidence source |
| `run_summary.json` | **Not found** in any window directory |

### Config sources

| File | Role |
|------|------|
| `config/strategies/paired_binary.yaml` | Strategy entry/stop/activation params |
| `config/scenarios/live_paired_binary_phase1_trailing_enforce.yaml` | Survival enforce profile + runtime/risk overrides |
| `config/risk/default.yaml` | Base risk (partially overridden) |
| `config/runtime/default.yaml` | Base runtime (scenario overrides most live settings) |

### Excluded from this report

Earlier failed `run_continue` attempts (`2240`, `2250`, `1725`, `1735`) and one-shot runs before 17:30 UTC — geoblock/FAK entry failures, not used for strategy diagnosis.

---

## 3. Effective Configuration Used

Effective config = `paired_binary.yaml` + `live_paired_binary_phase1_trailing_enforce.yaml` deep-merge (scenario wins on conflicts) + runtime/risk defaults where not overridden.

### Entry parameters

| Parameter | Effective value | Meaning | Code location | Session effect |
|-----------|-----------------|--------|---------------|----------------|
| `position_size` | `5` | Shares per leg | `paired_binary.yaml`, entry saga | All fills qty=5 (venue min) |
| `max_pair_entry_cost` | `1.015` | Max YES+NO entry sum | `entry_eval.py` | All entries at **1.01** (under cap) |
| `max_spread_yes/no` | `0.02` | Entry spread gate | `entry_eval.py` | Entries passed spread checks |
| `entry_order_style` | `FAK` | Immediate-or-cancel entry | entry saga | Both legs filled on first attempts |
| `allow_resting_entry_orders` | `false` | No GTC entry | strategy config | No resting entry behavior observed |
| `pair_entry_submit/fill_timeout_s` | `5` / `10` | Entry saga timeouts | pair entry | No timeout aborts in session |
| `activation_gap_retry_s` | `3` | Recheck window if activation gap too large | `activation_flow.py` | No `activation_rejected_loss_budget` facts |
| `activation_gap_max_retries` | `6` | Max activation rechecks | `activation_flow.py` | Not exercised |
| `reject_if_spread_exceeds_loss_budget` | `true` | Block entry if spread eats stop budget | `entry_eval.py` | Entries still at 1.01; spread within budget |

### Activation / loser-leg parameters

| Parameter | Effective value | Meaning | Code location | Session effect |
|-----------|-----------------|--------|---------------|----------------|
| Activation trigger | Both legs filled + sellable inventory | Arms `BOTH_LEGS_ACTIVE` | `lifecycle.activate_monitoring()` | `paired_binary_activation_reference` ~0s after fill |
| `pair_stop_loss_pct` | `0.09` | Loss budget = 9% of pair cost | `pnl.loss_budget()` | **loss_budget = 0.0909** on pair_cost 1.01 |
| `slippage_buffer` | `0.005` | Added to planned stop for trigger | `pnl.trigger_stop_price()` | trigger = planned_stop + 0.005 |
| Loser stop trigger | `bid <= trigger_stop` | Dual monitor | `evaluate_dual_stop()` | Fired 8–41s after activation |
| Dual-hit tie-break | Larger `(entry−bid)` loss, then wider spread, then NO | `choose_dual_stop_leg()` | Only if both triggers hit simultaneously — **not observed** in this session |
| Exit style | `FAK` urgent exit | Loser leg sell | `exit_engine.py` | Stop exits via FAK (venue reconciled fills in PnL facts) |

**Stop price math (example pair_cost 1.01, YES 0.50, NO 0.51):**

```
loss_budget = 1.01 × 0.09 = 0.0909
YES planned_stop = 0.50 − 0.0909 = 0.4091 → trigger 0.4141
NO  planned_stop = 0.51 − 0.0909 = 0.4191 → trigger 0.4241
```

Observed `loser_bid_at_stop` values (0.36–0.43) were at or below triggers — mechanically correct.

### Survivor parameters

| Parameter | Effective value | Meaning | Code location | Session effect |
|-----------|-----------------|--------|---------------|----------------|
| `survivor_floor.enabled` | `true` | Immediate post-stop floor | `survivor_floor.py` | Floor set within ~1–12s of stop |
| `survivor_floor.mode` | `winner_entry_price` | Floor = survivor entry + buffer | `compute_floor_price()` | Floor = **entry** (buffer 0) |
| `survivor_floor.floor_buffer` | `0.00` | Added to entry for floor |同上 | Hard floor **exactly at entry** |
| `survivor_floor.enforcement_mode` | `enforce` | Submit exit on breach | `advisory.py` | 5 hard-floor exits |
| `recovery_level.enabled` | `true` | Compute breakeven after loser exit | `recovery_level.py` | Breakeven **~0.58–0.69** (cash-based) |
| `recovery_level.desired_buffer` | `0.00` | Added to breakeven numerator | recovery_level | Full pair-cost recovery target |
| `recovery_level.activation_buffer` | `0.00` | Trailing arm threshold offset | trailing + recovery | Trailing arms when bid ≥ breakeven |
| `trailing_stop.enabled` | `true` | Post-recovery trailing | `trailing_stop.py` | Armed in 9/13 completed windows |
| `trailing_stop.enforcement_mode` | `enforce` | Submit on trail breach | enforcement | 5 trailing exits |
| `trailing_stop.activation_mode` | `loss_recovered` | Arm only if bid ≥ breakeven | `trailing_stop.py` L96–97 | No arm before ~0.60+ bid |
| `trailing_stop.trail_distance` | `0.20` | Absolute trail width | trailing_stop | Often **overridden by min_profit_lock** |
| `trailing_stop.min_profit_lock` | `0.01` | Min trail floor = entry + 1¢ | trailing_stop L105–107 | Effective trail ~**entry+0.01** until large peak |
| `trailing_stop.arm_delay_s` | `5.0` | Seconds after loser exit before arm | trailing_stop L93 | Trailing arms 8–50s after stop |
| `trailing_stop.max_spread` | `0.04` | Liquidity gate | trailing + floor | No spread-gate rejections in facts |
| `require_fresh_book` | `true` (floor + trail) | Quality gate | survival modules | Books passed (`quality_verdict: pass`) |
| `flatten_before_event_end_s` | `20` | Near-close lifecycle | `strategy_lifecycle` | Winners ran to resolution inside window |
| `trailing_stop.disable_near_close_s` | `30.0` | Disable new trail logic near close | trailing_stop | Not the limiting factor on most losses |

### Risk / runtime overrides (scenario)

| Parameter | Default | Effective (scenario) |
|-----------|---------|----------------------|
| `risk.notional.max_usd` | 4 | **15** |
| `risk.deployment.token_cap_usd` | 10 | **20** |
| `risk.deployment.portfolio_cap_usd` | 20 | **30** |
| `execution_mode` | shadow | **live** |

---

## 4. Session Timeline Overview

| UTC time | Event | Window |
|----------|-------|--------|
| 17:30:04 | Session started (`session_id` 51502182…) | — |
| 17:34:30 | Window run start | 1740 |
| 17:36:28 | Window complete (DONE) | 1740 |
| 17:39:30 → 17:40:40 | Run / complete | 1745 |
| 17:44:30 → 17:49:51 | Run / complete | 1750 |
| 17:49:51 → 17:53:01 | Run / complete | 1755 |
| 17:54:30 → 17:56:55 | Run / complete | 1800 |
| 17:59:30 → 18:04:41 | Run / complete | 1805 |
| 18:04:41 → 18:05:30 | Run / complete | 1810 |
| 18:09:30 → 18:14:51 | Run / complete | 1815 |
| 18:14:51 → 18:15:39 | Run / complete | 1820 |
| 18:19:30 → 18:21:52 | Run / complete | 1825 |
| 18:24:30 → 18:25:37 | Run / complete | 1830 |
| 18:29:30 → 18:30:27 | Run / complete | 1835 |
| 18:34:30 → 18:36:22 | Run / complete | 1840 |
| 18:39:30 | Window run start | 1845 |
| ~18:41:29 | Loop interrupted (`ONLY_NO_ACTIVE`) | 1845 |

**Aggregate:** 13 DONE windows in ~62 minutes; average ~4.8 min per window including orchestration gaps.

---

## 5. Per-Window Detailed Analysis

Legend: exit prices from `paired_binary_realized_pnl_tentative` (tentative where noted). `event_slug` from `session_log.jsonl`.

### 5.1 Window 1740

| Field | Value |
|-------|-------|
| event_slug | `btc-updown-5m-1783445700` |
| market_id | `btc_5m_20260707_1740` |
| entry timestamp | 2026-07-07T17:34:40Z |
| entry YES / NO | 0.50 / 0.51 |
| qty / pair cost | 5 / 1.01 |
| activation timestamp | 2026-07-07T17:34:40Z |
| activation bids YES / NO | 0.49 / 0.50 |
| stop leg / survivor | NO / YES |
| stop trigger / loser bid | 0.4241 / 0.42 |
| stop execution (NO exit) | **0.38** |
| survivor floor | 0.50 |
| recovery breakeven | 0.631 |
| trailing armed | 17:35:58Z, peak 0.64, trail floor at exit **0.53** |
| exit timestamp / reason | 17:36:17Z / `trailing_stop:trail_floor_breach` |
| survivor exit (YES) | **0.52** |
| resolution (final bids) | YES 0.51 / NO 0.48 |
| tentative PnL | **−$0.55** (−$0.11/pair) |

**Interpretation:** NO hit stop first (bid 0.42 ≤ 0.4241). YES recovered to 0.64 (above breakeven 0.631); trailing armed after 5s delay. Retrace to executable bid 0.51 breached trail floor 0.53 (min_profit_lock dominated). Exit mechanically consistent. YES later near 0.51 — exit did not clearly cut a resolution winner; loss driven by **early NO stop + tight trail after partial recovery**.

---

### 5.2 Window 1745

| Field | Value |
|-------|-------|
| event_slug | `btc-updown-5m-1783446000` |
| entry | 0.50 / 0.51 @ 17:39:40Z |
| stop leg / survivor | YES / NO |
| stop trigger / bid | 0.4141 / 0.41 |
| YES stop exit | **0.41** |
| floor / breakeven | 0.51 / 0.601 |
| trailing | armed 17:40:18Z, peak 0.62, trail floor 0.52 |
| exit | 17:40:29Z / **`survivor_floor:hard_floor_breach`** (NO @ **0.51**) |
| final bids | YES 0.50 / NO 0.49 |
| PnL | **−$0.45** |

**Interpretation:** Trailing triggered at bid 0.50 (trail_floor_breach fact) **same tick** as hard floor (bid ≤ 0.51). Enforced exit was **hard floor** at limit 0.50. Both modules overlapped; floor won dispatch. Market stayed ~50/50 — unclear favorable resolution for NO; exit protected near entry rather than cutting a clear winner.

---

### 5.3 Window 1750 ✓ winner

| Field | Value |
|-------|-------|
| event_slug | `btc-updown-5m-1783446300` |
| entry | 0.50 / 0.51 @ 17:44:40Z |
| stop leg / survivor | YES / NO |
| YES stop exit | **0.32** |
| floor / breakeven | 0.51 / 0.691 |
| trailing | armed 17:45:28Z, peak 0.70 — **no enforce exit** |
| exit reason | **held to resolution** |
| NO resolution exit | **0.97** |
| final bids | YES 0.03 / NO 0.96 |
| PnL | **+$1.40** |

**Interpretation:** Clear directional move: NO won binary. Survivor never breached floor/trail before resolution. YES stop was correct (YES collapsed). Winner because **market direction persisted**, not because stop leg was wrong.

---

### 5.4 Window 1755

| Field | Value |
|-------|-------|
| event_slug | `btc-updown-5m-1783446600` |
| entry | 0.49 / 0.52 @ 17:50:02Z |
| stop leg / survivor | YES / NO |
| YES stop / NO survivor exit | **0.39** / **0.41** |
| breakeven | 0.621 |
| trailing | peak path → trail floor **0.57** at exit; bid 0.50 triggered |
| exit | `trailing_stop:trail_floor_breach` |
| final bids | YES **0.58** / NO 0.41 |
| PnL | **−$1.05** (worst window) |

**Interpretation:** YES stopped at 0.39; YES **later recovered to 0.58** (stopped leg would have won resolution). Survivor NO exited at 0.41; NO stayed weak. Classic **whipsaw**: stop leg became eventual winner, survivor was loser. Loss from **survivor trailing exit + wrong-side survivor**, not stop math error.

---

### 5.5 Window 1800

| Field | Value |
|-------|-------|
| entry | 0.51 / 0.50 @ 17:54:41Z |
| stop / survivor | YES / NO |
| YES stop / NO exit | 0.39 / **0.56** (trail) |
| breakeven | 0.621 |
| exit | trailing @ bid ~0.585, floor 0.60 |
| final bids | YES 0.43 / NO 0.56 |
| PnL | **−$0.30** |

**Interpretation:** NO peaked ~0.70, trailed, exited 0.56 while still leading (NO 0.56 vs YES 0.43). Trail locked profit above entry but below breakeven cash recovery — **partial protection**, still net loss vs pair cost.

---

### 5.6 Window 1805 ✓ winner

| Field | Value |
|-------|-------|
| entry | 0.50 / 0.51 @ 17:59:41Z |
| stop / survivor | YES / NO |
| YES stop | **0.36** |
| trailing armed, no enforce exit | peak 0.66 |
| NO resolution | **0.99** |
| PnL | **+$1.70** (best window) |

**Interpretation:** Strong NO trend; survivor held. Trailing never forced exit before resolution.

---

### 5.7 Window 1810

| Field | Value |
|-------|-------|
| entry | 0.50 / 0.51 @ 18:04:52Z |
| stop / survivor | NO / YES |
| NO stop / YES exit | 0.39 / **0.50** (hard floor) |
| trailing | **not armed** (breach before recovery) |
| time stop→exit | **22s** |
| PnL | **−$0.60** |

**Interpretation:** YES survivor never reached breakeven ~0.621 before bid hit hard floor 0.50. Hard floor dominated; fast exit.

---

### 5.8 Window 1815 ✓ winner

| Field | Value |
|-------|-------|
| entry | 0.51 / 0.50 @ 18:09:41Z |
| stop / survivor | YES / NO |
| YES stop | 0.42 |
| trailing armed, no enforce | peak 0.69 |
| NO resolution | **0.87** |
| PnL | **+$1.40** |

**Interpretation:** Directional NO win; survival did not cut the trade.

---

### 5.9 Window 1820

| Field | Value |
|-------|-------|
| entry | 0.50 / 0.51 @ 18:15:01Z |
| stop / survivor | YES / NO |
| YES stop / NO exit | 0.40 / **0.44** (hard floor) |
| trailing | not armed |
| final bids | YES 0.55 / NO 0.44 |
| PnL | **−$0.85** |

**Interpretation:** Fast hard-floor exit 19s after stop. Final YES 0.55 suggests **YES became favored side** after NO survivor already exited — possible case of cutting before flip, but NO was still weak at end (0.44).

---

### 5.10 Window 1825

| Field | Value |
|-------|-------|
| entry | 0.51 / 0.50 @ 18:19:40Z |
| stop / survivor | NO / YES |
| NO stop / YES exit | 0.36 / **0.57** (trail) |
| peak / trail floor at exit | 0.71 / 0.55 |
| final bids | YES 0.54 / NO 0.45 |
| PnL | **−$0.40** |

**Interpretation:** YES recovered to 0.71 then trailed out at 0.57 — trail captured some gain vs entry 0.51 but below cash breakeven ~0.651.

---

### 5.11 Window 1830

| Field | Value |
|-------|-------|
| entry | 0.49 / 0.52 @ 18:24:40Z |
| stop / survivor | NO / YES |
| NO stop / YES exit | 0.43 / **0.45** (hard floor) |
| trailing | not armed |
| stop→exit | **11.6s** |
| PnL | **−$0.65** |

**Interpretation:** Hard floor at YES entry 0.49; no time for trailing recovery path.

---

### 5.12 Window 1835

| Field | Value |
|-------|-------|
| entry | 0.51 / 0.50 @ 18:29:40Z |
| stop / survivor | YES / NO |
| YES stop / NO exit | 0.43 / **0.44** (hard floor) |
| stop→exit | **6.8s** (fastest enforce) |
| PnL | **−$0.70** |

**Interpretation:** NO survivor breached floor 0.50 almost immediately after stop; no trailing arm.

---

### 5.13 Window 1840

| Field | Value |
|-------|-------|
| entry | 0.51 / 0.50 @ 18:34:41Z |
| stop / survivor | NO / YES |
| NO stop / YES exit | 0.37 / **0.52** (trail) |
| peak 0.65, trail floor 0.52 at trigger | bid 0.52 |
| PnL | **−$0.60** |

**Interpretation:** Trail triggered exactly at min_profit_lock level (entry 0.51 + 0.01).

---

### 5.14 Interrupted Window 1845

| Field | Value |
|-------|-------|
| event_slug | `btc-updown-5m-1783449600` |
| entry | 0.51 / 0.50 @ 18:39:40Z |
| stop leg / survivor | YES stopped / NO active |
| YES stop | trigger 0.4241, bid 0.40 (stop fired 18:40:04Z) |
| state at interrupt | **`ONLY_NO_ACTIVE`** (346 ticks) |
| trailing | armed 18:40:28Z, peak 0.67 |
| exit / PnL | **none** — no `survival_enforce_exit_submitted`, no PnL fact |
| last fact | `paired_binary_loop_interrupted` @ 18:41:29Z, NO bid ~0.61 |

**Interpretation:** Open survivor NO exposure when session/window run interrupted. Trailing armed with NO above breakeven; no forced exit recorded.

**Before next live run:** verify venue positions for NO token, reconcile orphaned inventory, confirm no pending orders, review why interrupt occurred (CTRL+C, process kill, window timeout).

---

## 6. Stop-Leg Selection Analysis

| Window | YES entry | NO entry | Loser bid @ stop | Stop leg | Survivor | Trigger | Stop exit | Stopped leg final bid | Stopped leg won resolution? |
|--------|-----------|----------|------------------|----------|----------|---------|-----------|----------------------|----------------------------|
| 1740 | 0.50 | 0.51 | NO 0.42 | NO | YES | 0.4241 | 0.38 | NO 0.48 | No |
| 1745 | 0.50 | 0.51 | YES 0.41 | YES | NO | 0.4141 | 0.41 | YES 0.50 | No |
| 1750 | 0.50 | 0.51 | YES 0.38 | YES | NO | 0.4141 | 0.32 | YES 0.03 | No (NO won) |
| 1755 | 0.49 | 0.52 | YES 0.39 | YES | NO | 0.4041 | 0.39 | YES **0.58** | **Yes** |
| 1800 | 0.51 | 0.50 | YES 0.41 | YES | NO | 0.4241 | 0.39 | YES 0.43 | No |
| 1805 | 0.50 | 0.51 | YES 0.36 | YES | NO | 0.4141 | 0.36 | YES n/a | No (NO won) |
| 1810 | 0.50 | 0.51 | NO 0.39 | NO | YES | 0.4241 | 0.39 | NO 0.49 | No |
| 1815 | 0.51 | 0.50 | YES 0.42 | YES | NO | 0.4241 | 0.42 | YES 0.12 | No (NO won) |
| 1820 | 0.50 | 0.51 | YES 0.40 | YES | NO | 0.4141 | 0.40 | YES **0.55** | **Yes** |
| 1825 | 0.51 | 0.50 | NO 0.40 | NO | YES | 0.4141 | 0.36 | NO 0.45 | No |
| 1830 | 0.49 | 0.52 | NO 0.43 | NO | YES | 0.4341 | 0.43 | NO 0.54 | No |
| 1835 | 0.51 | 0.50 | YES 0.40 | YES | NO | 0.4241 | 0.43* | YES 0.55 | **Yes** |
| 1840 | 0.51 | 0.50 | NO 0.39 | NO | YES | 0.4141 | 0.37 | NO 0.47 | No |

*1835 YES stop fill 0.43 vs bid 0.40 — slippage on FAK stop.

### Answers

| Question | Finding |
|----------|---------|
| Did bot stop the eventual winning leg? | **Sometimes after the fact** — 1755, 1820, 1835: YES stopped then YES bid recovered strongly. At trigger time, those legs were losers by bid vs entry. |
| YES stopped more often — parameter bias? | **No** — 9× YES / 4× NO matches which leg crossed **trigger_stop** first; thresholds symmetric around entry. |
| Symmetric stop calculation? | **Yes** — same `loss_budget` applied per leg (`pnl.compute_pair_pnl_budgets`). |
| Sensitive to small early moves? | **Moderate** — triggers ~9% below entry (~4–5¢); stops fired 9–41s after activation when bid moved ~8–10¢. |
| Stopped leg later recovered? | **1755, 1820, 1835** show clear YES recovery after YES stop; survivor NO/YES exits did not benefit. |

---

## 7. Survivor Exit Reason Analysis

### By reason group

| Group | Windows | Count | PnL sum |
|-------|---------|-------|---------|
| `trailing_stop → trail_floor_breach` | 1740, 1755, 1800, 1825, 1840 | 5 | −$2.90 |
| `survivor_floor → hard_floor_breach` | 1745, 1810, 1820, 1830, 1835 | 5 | −$3.25 |
| Held to resolution (no enforce) | 1750, 1805, 1815 | 3 | **+$4.50** |
| Interrupted | 1845 | 1 | n/a |

### Cross-window survivor table

| Window | Survivor | Entry | Breakeven | Peak | Floor | Trail floor @ exit | Exit reason | Exit price | Resolution bid | PnL |
|--------|----------|-------|-----------|------|-------|-------------------|-------------|------------|----------------|-----|
| 1740 | YES | 0.50 | 0.631 | 0.64 | 0.50 | 0.53 | trail | 0.52 | 0.51 | −0.55 |
| 1745 | NO | 0.51 | 0.601 | 0.62 | 0.51 | 0.52 | hard floor | 0.51 | 0.49 | −0.45 |
| 1750 | NO | 0.51 | 0.691 | 0.70 | 0.51 | — | resolution | 0.97 | 0.96 | +1.40 |
| 1755 | NO | 0.52 | 0.621 | 0.66 | 0.52 | 0.57 | trail | 0.41 | 0.41 | −1.05 |
| 1800 | NO | 0.50 | 0.621 | 0.70 | 0.50 | 0.60 | trail | 0.56 | 0.56 | −0.30 |
| 1805 | NO | 0.51 | 0.651 | 0.66 | 0.51 | — | resolution | 0.99 | 0.99 | +1.70 |
| 1810 | YES | 0.50 | 0.621 | — | 0.50 | — | hard floor | 0.50 | 0.50 | −0.60 |
| 1815 | NO | 0.50 | 0.591 | 0.69 | 0.50 | — | resolution | 0.87 | 0.87 | +1.40 |
| 1820 | NO | 0.51 | 0.611 | — | 0.51 | — | hard floor | 0.44 | 0.44 | −0.85 |
| 1825 | YES | 0.51 | 0.651 | 0.71 | 0.51 | 0.55 | trail | 0.57 | 0.54 | −0.40 |
| 1830 | YES | 0.49 | 0.581 | — | 0.49 | — | hard floor | 0.45 | 0.45 | −0.65 |
| 1835 | NO | 0.50 | 0.581 | — | 0.50 | — | hard floor | 0.44 | 0.44 | −0.70 |
| 1840 | YES | 0.51 | 0.641 | 0.65 | 0.51 | 0.52 | trail | 0.52 | 0.52 | −0.60 |

### Group analysis

**Trailing stop (`trail_floor_breach`):**

- **Parameter cause:** `min_profit_lock=0.01` keeps trail floor near entry until peak ≥ entry + trail_distance + lock (~0.72 for entry 0.51); retracements of 10–15¢ from peak trigger exit.
- **Mathematically correct:** Yes — trigger facts show `executable_bid ≤ trail_floor`.
- **Recovery after exit:** Mixed; 1800 NO still led at 0.56; 1755 NO stayed weak.
- **Cut winners?** 1740/1840 ambiguous; 1755 cut loser survivor.

**Hard floor (`hard_floor_breach`):**

- **Parameter cause:** `floor_buffer=0` → floor = entry; any dip to entry exits.
- **Mathematically correct:** Yes — e.g. 1745 NO bid 0.50 ≤ floor 0.51.
- **Overlap with trailing:** 1745 both triggered; hard floor dispatched. Floor and min_profit_lock trail are **~1¢ apart** — they fight/overlap by design.

**Held to resolution:**

- Survivor never breached enforce thresholds before binary paid ~$1.
- **Required:** directional persistence + enough time before near-close flatten.

---

## 8. Parameter-to-Outcome Mapping

| Parameter | Expected mechanical effect | Observed in session | Risk / interpretation |
|-----------|---------------------------|---------------------|------------------------|
| `position_size=5` | Fixed clip size | All trades qty 5 | Losses scale linearly; small absolute $ |
| `max_pair_entry_cost=1.015` | Cap entry sum | All at **1.01** | ~1% structural headwind vs $1.00 fair pair |
| `entry_order_style=FAK` | Immediate fill or fail | Clean dual fills | No entry rejects in session |
| `pair_stop_loss_pct=0.09` | ~9% pair-cost stop band | loss_budget 0.0909 | Stops fired predictably; not main loss driver alone |
| `slippage_buffer=0.005` | Widens trigger 0.5¢ | Triggers 0.4141/0.4241 | Small effect vs 9% band |
| `survivor_floor floor_buffer=0` | Hard floor at entry | 5 fast exits at entry | **Major loss driver** on choppy survivors |
| `recovery_level buffers=0` | Breakeven ~0.58–0.69 | Computed correctly | Survivors rarely sustained above before floor/trail |
| `trailing_stop.trail_distance=0.20` | 20¢ trail width | **Often inactive** due to min_lock | Effective trail much tighter than 0.20 |
| `trailing_stop.min_profit_lock=0.01` | Floor trail at entry+1¢ | Trail floors 0.52 on 0.51 entry | **Dominates trail behavior** in this sample |
| `trailing_stop.activation_mode=loss_recovered` | Arm only if bid ≥ breakeven | Armed after ~0.60+ | Correct gating; still exits on small retraces |
| `trailing_stop.arm_delay_s=5` | Delay post-stop | 8–50s to arm | Hard floor often fires first (1810, 1820, 1830, 1835) |
| `require_fresh_book=true` | Block stale exits | All exits `quality_verdict: pass` | No stale-book false triggers observed |
| `flatten_before_event_end_s=20` | Force flatten near close | Winners reached resolution inside window | Losers exited earlier via survival |

---

## 9. Winners vs Losers Comparison

| Dimension | Winners (1750, 1805, 1815) | Losers (10 windows) |
|-----------|------------------------------|---------------------|
| Pair cost | 1.01 | 1.01 (same) |
| Stop leg | YES all 3 | YES 6 / NO 4 |
| Survivor | NO all 3 | Mixed YES/NO |
| Survivor peak | 0.66–0.70+ | Similar peaks in trail losers |
| Recovery breakeven reached? | Bid exceeded breakeven, **no breach before resolution** | Often peaked then retraced, or never reached breakeven before hard floor |
| Trailing armed? | Yes (all 3) | Yes on 5 losers; no on 4 hard-floor losers |
| Hard floor hit? | No | Yes on 5 losers |
| Time activation→stop | 21–30s | 9–41s (similar) |
| Time stop→exit | **271–279s** (held long) | **7–156s** (cut early) |
| Resolution clarity | NO → 0.87–0.99 | Often ~0.45–0.56 (contested) |
| Enforce exit | **None** | All 10 had enforce exit |

**Separator:** Winners held survivor through **one-sided resolution** without floor/trail breach. Losers had **choppy two-sided books** where survivor briefly recovered then retraced to entry — exactly where floor + tight trail fire.

---

## 10. Key Failure Modes Observed

1. **Hard floor at entry (`floor_buffer=0`)** — exits within 7–22s of stop on 4 windows without trailing ever arming.
2. **min_profit_lock-dominated trailing** — effective trail ~1¢ above entry, not 20¢; exits on normal retracements after partial recovery.
3. **Hard floor / trailing overlap** — 1745 both triggered; ~1¢ separation creates redundant/conflicting signals.
4. **Whipsaw (1755)** — stopped leg recovered to win resolution; survivor was wrong side; largest loss.
5. **Pair cost drag** — 1.01 entry vs ~0.212 desired profit requires ~21¢ survivor move; exits often near 0.44–0.57.
6. **1845 interrupt** — open NO survivor; operational not strategy.
7. **Missing `run_summary.json`** — per-window summaries not materialized; analysis relied on `facts.jsonl`.

No evidence of **stale book** or **spread-gate false stops** on loser-leg triggers; book quality passed on enforced exits.

---

## 11. Questions Before Tuning

1. Is **`floor_buffer=0`** intentional (must not trade below entry) or should floor reference **cash breakeven (~0.60+)** instead of entry?
2. Should **`min_profit_lock=0.01`** be reduced/disabled so **`trail_distance=0.20`** actually governs retracements?
3. When hard floor and trailing both fire, is **floor priority** correct, or should the looser threshold win in chop?
4. Is **pair_stop_loss_pct=0.09** appropriate for 5m BTC volatility given whipsaw cases (1755)?
5. Should **`max_pair_entry_cost`** target **≤1.00** to reduce structural drag (accept fewer entries)?
6. For **`1845`**, what interrupt policy should `run_continue` use — flatten survivor before next window?
7. Do we require **reconciled PnL** before tuning, given all session PnL is `tentative`?

---

## 12. Future Recommendations, No Implementation Yet

*Non-implemented recommendations only — for future design discussion.*

1. **Advisory replay on this session** — rerun survival modules offline on stored books to sweep `floor_buffer`, `min_profit_lock`, and `trail_distance` before live changes.
2. **Separate hard floor from entry** — e.g. floor at `breakeven − ε` or `entry − loss_budget/2` to avoid immediate exit on first touch.
3. **Relax min_profit_lock** — let 0.20 trail distance operate after recovery; current config effectively ignores 0.20 until peak > ~0.72.
4. **Enforce exit priority policy** — document whether floor or trail wins when both breach same tick (1745 pattern).
5. **`run_summary.json` generation** — aggregate per-window table for faster operational review.
6. **`run_continue` interrupt handler** — on session stop, complete survivor flatten or log explicit open-risk manifest (1845).
7. **`--continue-on-window-failure`** — operational resilience (geoblock/FAK); separate from strategy tuning.

---

## Appendix: Explicit Answers to Required Questions

| # | Question | Answer |
|---|----------|--------|
| 1 | Stop-leg wrong vs survivor too aggressive? | **Survivor exits too aggressive** relative to pair-cost economics; stop-leg logic was mechanically correct. |
| 2 | Hard floor protect or cut favorable legs? | **Mixed** — protected in chop (1835); 1745/1820 ambiguous; did not cause winner cuts (winners had no hard floor exit). |
| 3 | Trailing after meaningful recovery or too early? | **After partial recovery** (peak 0.62–0.71) but exit threshold **too tight** due to min_profit_lock. |
| 4 | `trail_distance=0.20` too tight/loose? | **Impossible to judge as configured** — min_profit_lock overrides; effective trail ~1¢ not 20¢. |
| 5 | `max_pair_entry_cost≈1.01` structural drag? | **Yes** — ~$0.05/pair vs $1.00; breakeven cash ~0.60+ vs entry ~0.50. |
| 6 | Winners because survivor not stopped or clearer direction? | **Both** — no enforce exit **and** strong one-sided resolution (NO 0.87–0.99). |
| 7 | Symmetric YES/NO? | **Parameter-symmetric**; YES stopped more due to **market path**, not config bias. |
| 8 | Poor book quality exits? | **No evidence** in this session. |
| 9 | Stale book / liquidity gap on stops? | **Not observed** on loser stops; FAK stop slippage normal. |
| 10 | 1845 state + pre-next-run checks? | **`ONLY_NO_ACTIVE`**, NO survivor armed trailing, **no flatten** — verify venue inventory/orders before live. |

---

*Report generated from live run artifacts only. All PnL figures are `tentative` per facts; venue reconciliation recommended before parameter changes.*
