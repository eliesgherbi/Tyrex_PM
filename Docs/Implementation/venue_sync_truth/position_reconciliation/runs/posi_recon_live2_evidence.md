# posi_recon_live2 — Position reconciliation live evidence report

**Run id:** `posi_recon_live2`  
**Question:** Can the bot reliably maintain cache, positions, and account balances that match venue truth so strategies can trust their reads?

**Artifacts:**

- `var/reporting/runs/posi_recon_live2/manifest.json`
- `var/reporting/runs/posi_recon_live2/facts.jsonl`
- `logs/live/posi_recon_live2_tyrex.log`
- `logs/live/posi_recon_live2_nautilus.log`

---

## Validation gates (must pass before treating the run as a gate)

### Loaded config: reconciliation live, not shadow

From the first `config_snapshot` row in `facts.jsonl`:

- **`position_reconciliation_enabled`: true** — appears in the JSON string on line 2.
- **`position_reconciliation_shadow_mode`: false** — same row.

Cite: `var/reporting/runs/posi_recon_live2/facts.jsonl` **line 2** (`config_snapshot`, `config_json` contains both keys).

### Nautilus version ≥ 1.225

Cite: `logs/live/posi_recon_live2_nautilus.log` **line 45**:

`nautilus_trader: 1.225.0`

### Race B fix present in source; run log did not prove the branch

The Race B bypass log line exists in deployed source:

```641:646:src/tyrex_pm/runtime/wallet_sync.py
                            _LOG.info(
                                "event=position_reconciliation_ts_last_skipped "
                                "component=wallet_sync reason=reconciliation_origin "
                                "instrument_id=%s",
                                iid,
                            )
```

**Tyrex process log:** substring `position_reconciliation_ts_last_skipped` occurs **0** times in `logs/live/posi_recon_live2_tyrex.log` (verified by full-file scan).  
**Interpretation:** Per your constraint (“absence is not confirmation”), this means **the log line did not fire**, not that the fix is absent. It **does not** satisfy the Part 1 sub-goal “at least one `ts_last_skipped` proves the branch in production.”

**Conclusion on gates:** Config and Nautilus version pass. Race B fix is **present in code** but **not evidenced by this run’s Tyrex log**.

---

## Run shape (limits Part 7)

From `var/reporting/runs/posi_recon_live2/manifest.json`:

| Field | Value |
| --- | --- |
| `started_at_utc` | `2026-04-15T19:44:21.920199+00:00` |
| `ended_at_utc` | `2026-04-15T20:09:47.802334+00:00` |
| Wall duration | ~25 minutes |
| `data_quality.run_ended_cleanly` | **false** |
| `data_quality.facts_incomplete` | **true** |
| `shutdown_drain_timed_out` | **true** |
| `shutdown_residual_orders` | 3 TX ids |

This is **not** a multi-hour soak; there is **no** 30+ minute idle window in the artifact window, and shutdown did not complete cleanly within the drain budget.

---

## Part 1 — Synthetic close path

### Tyrex: apply + synthetic + fill_sent (shadow off)

All of the following are **INFO** lines without per-line timestamps in `posi_recon_live2_tyrex.log` (file uses preamble lines without ISO timestamps; correlate with `facts.jsonl` for wall time).

| Step | `posi_recon_live2_tyrex.log` line | Evidence |
| --- | ---: | --- |
| Queue | **57** | `event=position_reconciliation_action_queued` … `venue_qty=0` `cache_qty=9.958500` `direction=close` |
| Apply | **58** | `event=position_reconciliation_apply_begin` … `shadow_mode=False` |
| Synthetic | **59** | `event=synthetic_close_begin` … **`strategy_id=EXTERNAL`** … `delta_qty=9.9585` |
| Fill sent | **60** | `event=position_reconciliation_fill_sent` … **`position_id=...POLYMARKET-EXTERNAL`** |
| Queue | **233** | `event=position_reconciliation_action_queued` … `venue_qty=0` `cache_qty=20.000000` |
| Apply | **234** | `event=position_reconciliation_apply_begin` … `shadow_mode=False` |
| Synthetic | **235** | `event=synthetic_close_begin` … **`strategy_id=CopyStrategy-000`** … `delta_qty=20.0` |
| Fill sent | **236** | `event=position_reconciliation_fill_sent` … **`position_id=...CopyStrategy-000`** |

### Nautilus: engine saw the second synthetic as a normal strategy fill

For the **722…** instrument, Nautilus logs a reconciliation-style `OrderFilled` and immediate `PositionClosed` with **`position_id` ending in `CopyStrategy-000`**:

Cite: `logs/live/posi_recon_live2_nautilus.log` **lines 1640–1641** (timestamps `2026-04-15T19:50:48.691271200Z` / `…691327300Z`).

### Part 1 checklist vs your expectations

| Expectation | Outcome |
| --- | --- |
| `apply_begin` after manual close, `shadow_mode=false` | **Yes** (Tyrex lines **58**, **234**). |
| `synthetic_close_begin` immediately after | **Yes** (lines **59**, **235**). |
| `synthetic_close_begin` uses **real** strategy id, **not** EXTERNAL | **Only for the second close**. First close uses **`EXTERNAL`** (line **59**). |
| `position_reconciliation_fill_sent` | **Yes** (lines **60**, **236**). |
| `position_reconciliation_ts_last_skipped` ≥ 1 | **No** — **0** occurrences in Tyrex log this run. |

**Why the first synthetic used `EXTERNAL`:** `wallet_sync` takes `original_strategy_id` from `cache.positions_open(instrument_id=…)[0].strategy_id` (see `src/tyrex_pm/runtime/wallet_sync.py` **lines 705–724**). At startup, Nautilus generated an inferred `OrderFilled` with **`position_id=...POLYMARKET-EXTERNAL`** for this instrument (Nautilus **line 648**), establishing the open position under **`EXTERNAL`**. The first synthetic close correctly inherited that id.

**Stop rule from your brief:** You asked to stop Part 1 if `synthetic_close_begin` does not appear after manual closes — it **does** appear (lines **59**, **235**). The **`EXTERNAL` strategy id on the first close** is a **separate deviation** from the “CopyStrategy-only smoking gun” expectation, not an absence of synthetic close.

---

## Part 2 — Position state truth (manual UI closes)

**Operator-supplied “manual click” timestamps are not in the artifacts.** The table uses the **`position_reconciliation` fact `recorded_at_utc`** when Tyrex observed `venue_qty=0` with non-zero `cache_qty` (close diff), which is the same instant recorded on the paired `wallet_sync` row.

### Instrument A — `0xb911de51…61268636628775057487750851354292290487617727323706281715857712570412110022658.POLYMARKET`

| Column | Value | Citation |
| --- | --- | --- |
| Manual close timestamp (proxy) | `2026-04-15T19:46:48.742521+00:00` | `facts.jsonl` **line 1269** (`position_reconciliation`) |
| Instrument | full id on line 1269 | same |
| Pre-close cache qty | `9.958500` | `facts.jsonl` **line 1269** (`cache_qty`) |
| Pre-close venue qty | `0` | `facts.jsonl` **line 1269** (`venue_qty`) |
| Synthetic close fill timestamp (Tyrex) | Same window as facts line 1269–1271; Tyrex log lines **57–60** (no ISO timestamp on those lines) | `posi_recon_live2_tyrex.log` **57–60** |
| Synthetic `position_id` | `…12110022658.POLYMARKET-**EXTERNAL**` | `posi_recon_live2_tyrex.log` **line 60** |
| Post-close cache qty | **Not directly recorded** in a follow-up `position_reconciliation` row for this instrument in `facts.jsonl` (only lines **1269**–**1271**). | Absence noted; see Nautilus discussion below. |
| Post-close venue qty | **Not re-stated** on a later fact row for this instrument. | — |
| Cache Position object count | **Cannot be asserted from facts** (no per-instrument position count field). | — |
| Clean vs accidental | **Tyrex mechanism** fired with **`EXTERNAL`** strategy id inherited from startup inferred fill (**Nautilus line 648**). This is **not** the “accidental engine zombie pair on this instrument at close time” pattern: **no** second `Generated inferred OrderFilled` for `b911…` appears after startup through the end of the Nautilus log (only **lines 641–649** reference that instrument for inferred reconciliation at **19:44:48**). | `posi_recon_live2_nautilus.log` **lines 641–649**, **930** |

**Nautilus evidence gap (important):** There is **no** `OrderFilled` / `PositionClosed` log line for `b911…` at **19:46:48** despite Tyrex `position_reconciliation_fill_sent` (**Tyrex line 60**). A full-file scan for `9.9585` only finds startup and the **19:46:30** venue poll (**Nautilus lines 191, 641–649, 736, 930**). **Do not** treat that absence as “fill did not happen”; treat it as “**not logged at INFO for this instrument/time**.”

### Instrument B — `0x7223123398…9989408815251.POLYMARKET`

| Column | Value | Citation |
| --- | --- | --- |
| Manual close timestamp (proxy) | `2026-04-15T19:50:48.689658+00:00` | `facts.jsonl` **line 3420** |
| Pre-close cache qty | `20.000000` | line **3420** |
| Pre-close venue qty | `0` | line **3420** |
| Venue partial mismatch earlier | `venue_qty=19` `cache_qty=20` **deferred** | `facts.jsonl` **line 2353** at `2026-04-15T19:49:18.744561+00:00` |
| Synthetic fill (Nautilus) | `2026-04-15T19:50:48.691271200Z` | `posi_recon_live2_nautilus.log` **line 1640** |
| Synthetic `position_id` | `…9408815251.POLYMARKET-**CopyStrategy-000**` | **line 1640** |
| Post-close | `PositionClosed` … `signed_qty=0.0` | **line 1641** |
| Clean vs accidental | **Tyrex / engine path presents as a single closing `OrderFilled` + `PositionClosed` on `CopyStrategy-000`**, not a LONG/SHORT pair on that instrument in the shown window. | **lines 1640–1641** |

---

## Part 3 — Account / cash truth

### What the facts actually contain

- `wallet_sync` rows (**example:** `facts.jsonl` **line 13**, **line 1270**, **line 9782**) report **counts** (`positions_fetched`, `orders_fetched`, `condition_ids_*`) and HTTP ok flags — **not** “Data API cash” or `outstanding_orders_value` scalars.
- `account_snapshot` rows **do** carry `py_clob_balance_usd`, `nautilus_cash_free_usd`, `nautilus_balances_json`, `capital_balance_abs_discrepancy_usd`, etc.

**Therefore:** the Part 3 table cannot be filled exactly as written (“Polymarket Data API cash / outstanding_orders_value from wallet_sync facts”) without **extrapolating beyond the schema**. Below, **venue cash proxy = `py_clob_balance_usd`** (CLOB balance path embedded in the snapshot), and **Nautilus cash** is split into:

1. **`nautilus_cash_free_usd`** as emitted by Tyrex (`balance_canonical_usd` tracks it).
2. **Last `AccountState` inside `nautilus_balances_json`** when the JSON is **parseable** (early snapshots).

### `nautilus_cash_free_usd` vs last account event (early run, parseable JSON)

At `account_snapshot` **`facts.jsonl` line 1267** (`captured_at_utc` `2026-04-15T19:46:32.053318+00:00`, immediately before the reconciliation fact at **1269**):

- `nautilus_cash_extract_note`: **`multiple_usdc_free_summed`**
- `nautilus_cash_free_usd`: **127.965691**
- Last balance inside `nautilus_balances_json`: **`free=39.990697`**, **`locked=0`** (third `AccountState` event in that JSON)
- `py_clob_balance_usd`: **43.987497**
- `capital_balance_abs_discrepancy_usd`: **83.978194**, `capital_balance_sources_disagree`: **true**

At **`facts.jsonl` line 1277** (`captured_at_utc` `2026-04-15T19:46:50.084158+00:00`, immediately after close fact **1269**):

- `nautilus_cash_free_usd`: **still 127.965691** (unchanged at the summarized-field level)
- Last `AccountState` in JSON: **still `free=39.990697`** (same three events as line 1267)
- `py_clob_balance_usd`: **39.990697** (↓ **4.000000** vs line 1267)

**Interpretation:** **`py_clob_balance_usd` moved coherently with the latest on-chain/CLOB refresh**, while **`nautilus_cash_free_usd` remained stuck at the summed multi-event value** across that bracket. That is **not** “Nautilus and venue agree within cents” if a strategy reads **`balance_canonical_usd` / `nautilus_cash_free_usd`**.

### Mid-run and end snapshots (truncated JSON in facts)

`account_snapshot` **`facts.jsonl` line 5161** (`account_snapshot_seq` **550**, `captured_at_utc` `2026-04-15T19:54:14.964255+00:00`):

- `py_clob_balance_usd`: **43.247758**
- `nautilus_cash_free_usd`: **550.636657**, note **`multiple_usdc_free_summed`**
- `nautilus_balances_json` length **2048** characters and **`json.loads` fails** (`Expecting ',' delimiter` at char ~2045) — the row appears **truncated** in JSONL.

`account_snapshot` **`facts.jsonl` line 9608** (last row in file):

- `py_clob_balance_usd`: **42.931623**
- `nautilus_cash_free_usd`: **1017.112275**, note **`multiple_usdc_free_summed`**
- `nautilus_balances_json` **also fails JSON parse** in the same way.

### Part 3 table (honest reconstruction)

| Timestamp (UTC) | Nautilus “canonical” free (`nautilus_cash_free_usd`) | Last `AccountState` free in JSON (if parseable) | Nautilus locked (last event) | Venue cash proxy (`py_clob_balance_usd`) | Data API outstanding orders | Tyrex `account_snapshot` line | Δ canonical vs venue |
| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| `2026-04-15T19:44:49.191938+00:00` | 43.987497 | 43.987497 | 0 | 43.987497 | **Not in facts** | **20** | 0.0 |
| `2026-04-15T19:54:14.964255+00:00` | 550.636657 | **unavailable (JSON truncated)** | **unavailable** | 43.247758 | **Not in facts** | **5161** | 507.388899 |
| `2026-04-15T20:08:38.913739+00:00` | 1017.112275 | **unavailable (JSON truncated)** | **unavailable** | 42.931623 | **Not in facts** | **9608** | 974.180652 |

**After the first synthetic close bracket (lines 1267 → 1277):** **`py_clob` moved −4.000000** while **`nautilus_cash_free_usd` did not move** — exactly the “silent cash read” risk class you called out, **for the canonical summarized field**.

---

## Part 4 — Order state truth

### Representative Tyrex-submitted order with full fact trail

**`client_order_id=TXe1de5abcff1a41be72e4ee2727`** (`0x7223123398…5251.POLYMARKET`):

| Time (UTC) | Fact | Line |
| --- | --- | ---: |
| `2026-04-15T19:49:02.154227+00:00` | `order_lifecycle` `OrderInitialized` | **2103** |
| `…02.313806+00:00` | `SUBMITTED` | **2110** |
| `…02.502878+00:00` | `ACCEPTED` | **2112** |
| `…05.045821+00:00` | `fill` + `order_lifecycle` `OrderFilled` `last_qty=19.000000` `last_px=0.06` | **2175–2176** |
| `…11.064065+00:00` | `fill` + `OrderFilled` (second partial) | **2262–2263** |

Nautilus confirms submit/accept/fill path around **lines 1278–1302** (`Submit LimitOrder(BUY 20…)`, `OrderSubmitted`, `OrderAccepted`, venue `Trade … MATCHED`, `OrderFilled`).

### Synthetic close order cleanup

Synthetic closing order **`bc48cb76-a0e3-45ab-8262-ab8f8c8cc495`** appears in Nautilus **line 1640** as an `OrderFilled` on **`CopyStrategy-000`**.

**Facts:** `grep`/JSONL scan shows **no** `order_lifecycle` rows containing `bc48cb76` in `facts.jsonl`.  
**Interpretation:** Reporting may not emit lifecycle facts for synthetic client orders — **absence is not proof** they remain in `Cache.orders_open()`; it **is** proof they are **not** visible in the reporting sink.

---

## Part 5 — Dual mechanism observation

### Instrument A (`b911…`)

| Mechanism | Count in window | Citation |
| --- | ---: | --- |
| Tyrex `synthetic_close_begin` | **1** | `posi_recon_live2_tyrex.log` **line 59** |
| Engine `Generated inferred OrderFilled` with `…EXTERNAL` | **1** at startup (**not** at manual close time) | `posi_recon_live2_nautilus.log` **line 648** |
| Engine inferred fill at **19:46:48** | **0** located in Nautilus log | Scan for `b911…` yields **8** lines total; **none** after **line 930** |

**Ordering:** Startup engine inferred (**19:44:48**) precedes live trading; manual close / Tyrex synthetic window (**~19:46:48** facts **1269**) shows **Tyrex-only** close activity in Tyrex logs (**57–60**) without a matching Nautilus inferred line for that instrument.

**Pattern:** Closest to **Pattern A** for the **manual close epoch** (Tyrex closes; **no** second EXTERNAL inferred fill located for `b911…` at that time), with the caveat that **Nautilus did not log the synthetic fill for this instrument** at INFO.

### Instrument B (`722…`)

| Mechanism | Observation | Citation |
| --- | --- | --- |
| Tyrex synthetic | **1** (`synthetic_close_begin`) | `posi_recon_live2_tyrex.log` **line 235** |
| Engine `Generated inferred OrderFilled` for `722…` | **None found** | Python scan: **0** `Generated inferred` lines containing `7223123398` |
| Nautilus `OrderFilled` closing the cache | **1**, `client_order_id=bc48cb76-…` | `posi_recon_live2_nautilus.log` **line 1640** |

**Pattern:** **Pattern A**-like for this instrument in the log window: **Tyrex-driven synthetic** shows up as a single closing fill on **`CopyStrategy-000`**, without a separate `…EXTERNAL` inferred fill for `722…`.

---

## Part 6 — Strategy trust scenario (722 instrument, ~19:50:48Z)

**Moment:** Immediately after external/manual sell activity and Tyrex reconciliation:

1. **Venue / wallet sync:** `wallet_sync` at **`2026-04-15T19:50:48.689658+00:00`** shows `positions_fetched: 2`, `orders_fetched: 1` — `facts.jsonl` **line 3421** (immediately after `position_reconciliation` **line 3420**).
2. **Nautilus portfolio:** **`OrderFilled` + `PositionClosed`** on **`CopyStrategy-000`** (**Nautilus lines 1640–1641**) — a strategy reading **positions** via the engine path would see **flat** on that instrument at that timestamp.
3. **Risk / deployment:** Immediately after, **`risk_decision`** rows still show **`risk_portfolio_deployment_exceeded`** for other signals (e.g. `facts.jsonl` **line 3452** at `2026-04-15T19:50:51.104732+00:00`, `portfolio_deploy_at_eval: 4.00000076`). That **denial is driven by deployment caps**, not by “still long 20 shares” on the closed instrument.

**Would the strategy be “wrong” if it trusted cache?**

- **Position read (this instrument):** **Aligned** with venue zero net position at the reconciliation instant per **`venue_qty=0`** on **line 3420** and **`PositionClosed`** on **Nautilus line 1641**.
- **Account read:** If the strategy used **`nautilus_cash_free_usd` / `balance_canonical_usd`**, it would see values **hundreds of USD away from `py_clob_balance_usd`** mid/late run (**lines 5161**, **9608**), i.e. **wrong for venue cash** under the reporting fields as emitted.

---

## Part 7 — Long-running stability (what this run can and cannot say)

| Topic | Finding | Citation |
| --- | --- | --- |
| Duration | ~**25 minutes** | `manifest.json` `started_at_utc` / `ended_at_utc` |
| Quiet period 30+ min | **Not observed** | Window too short |
| Network blip | **Not isolated** in this evidence pass | No dedicated marker extracted |
| `wallet_sync` growth | `condition_ids_cache` **25** at start (**line 13**) and end (**line 9782**) | Stable in this metric |
| `wallet_sync` cycles | **100** at last sync | `facts.jsonl` **line 9782** |
| Health at end | `tradable_state_health` **`healthy`** / `nautilus_exec_startup_reconciliation_complete` | `facts.jsonl` **line 9599** (last such row) |
| Shutdown | **`run_ended_cleanly: false`**, drain **timed out**, **3** residual orders | `manifest.json` |

**`_sync_cycle` / `_apply_reconciliation_actions` timing:** not extracted from logs in this pass — **gap** (would require structured timing logs or profiling).

---

## Part 8 — Account propagation after synthetic close (722 instrument, strong evidence)

Bracket the synthetic close using Nautilus `Portfolio: Updated AccountState` and the synthetic `OrderFilled`:

1. **`2026-04-15T19:50:45.119068600Z`:** `Updated AccountState` … **`free=41.366738 USDC.e`**, **`locked=0`** — `posi_recon_live2_nautilus.log` **line 1639** (immediately follows “Checking account balance” after trade confirmation activity).
2. **`2026-04-15T19:50:48.691271200Z`:** Synthetic `OrderFilled` **SELL 20 @ 0.50** on **`CopyStrategy-000`** — **line 1640**.
3. **`facts.jsonl` line 3444** (`captured_at_utc` `2026-04-15T19:50:51.100225+00:00`): `py_clob_balance_usd` **41.366738** matches the **Nautilus** `AccountState` free shown at **line 1639** (no double-count mismatch *between those two*).

**Did synthetic close “free locked collateral” from a position?**  
For this instrument’s path, **locked stayed at 0 before and after** in the shown `AccountState` (**line 1639**). The more important observation is **`free` changed coherently with venue polling (`py_clob`) around the manual trade**, and the **`OrderFilled` at line 1640** is immediately followed by **`PositionClosed` at line 1641**.

**Contrast — first close bracket (Part 3):** canonical **`nautilus_cash_free_usd` did not move** across **`facts.jsonl` lines 1267 → 1277** while **`py_clob_balance_usd` did**.

---

## Aggregate conclusion (single paragraph)

**No — not as a universal foundation for strategies that read “account / free cash” from the same fields Tyrex currently elevates as canonical when `nautilus_cash_extract_note` is `multiple_usdc_free_summed`.** This run **does** show **`position_reconciliation_apply_begin` / `synthetic_close_begin` / `position_reconciliation_fill_sent` with `shadow_mode=false`**, and for the **`722…`** instrument it shows a **clean `CopyStrategy-000` synthetic `OrderFilled` and `PositionClosed` in Nautilus** (**lines 1640–1641**) consistent with **`venue_qty=0`** in **`facts.jsonl` line 3420**, which is **good evidence that the position layer can be brought back in line after external sells**. But the **first synthetic close carried `EXTERNAL` ids** because startup **`Generated inferred OrderFilled`** (**Nautilus line 648**) attached the position to **`EXTERNAL`**, contradicting the “always `CopyStrategy-000`” smoking-gun expectation; and **canonical cash fields diverged massively from `py_clob_balance_usd` mid/late run** (**`facts.jsonl` lines 5161 and 9608**) with **`nautilus_balances_json` truncation** that prevents independent JSON verification from facts alone. **`position_reconciliation_ts_last_skipped` never appeared in the Tyrex log**, so the Race B “reconciliation_origin ts_last” branch was **not demonstrated in production by logging**, even though the code path exists (**`wallet_sync.py` lines 641–646**). Finally, **`run_ended_cleanly` is false** with **drain timeout and residual orders** (`manifest.json`), so operational completeness is not established.

**Triage label (your three-option framing):** **Yes for positions and orders, no for account state** — *as represented by `nautilus_cash_free_usd` / `balance_canonical_usd` alongside `py_clob_balance_usd` in this run* — with the additional limitation that **`EXTERNAL`-tagged positions from startup inferred fills** drive the first synthetic close’s `strategy_id`, not `CopyStrategy-000`.

---

## Evidence gaps (explicit)

1. **No ISO timestamps on Tyrex reconciliation lines 57–60 / 233–236** — correlate via `facts.jsonl` rows **1269–1271** and **3420–3422**.
2. **No `position_reconciliation_ts_last_skipped` lines** in Tyrex log this run.
3. **`nautilus_balances_json` truncated** on later `account_snapshot` rows (parse failures at **lines 3438+**, **5161**, **9608**).
4. **No Nautilus INFO `OrderFilled` located for `b911…` at ~19:46:48** despite Tyrex `fill_sent` (**Tyrex line 60**).
5. **`wallet_sync` facts do not include Data API cash / outstanding order value scalars** — cannot populate your Part 3 template literally without new fields or external API pulls.
