# Phase 2 — Baseline metrics (M0)

**Audit date:** 2026-06-29  
**Config hashes:** see `baseline_runs.yaml`  
**Git at audit:** `ac5191d76aa26eea0c93cf8f004c0ba3e6cdf44e`

## Artifact verification summary

| Run ID | Facts path | Status | Notes |
|--------|------------|--------|-------|
| `paired_binary_live_1782741234` | `var/reporting/runs/.../facts.jsonl` | **MISSING** | Metrics below **RECONSTRUCTED** from prior live forensics (same workspace session) |
| `paired_binary_live_1782742788` | present (79 facts, 86 KB) | **VERIFIED** | Parsed directly during M0 |
| `paired_binary_live_1782737121` | `var/reporting/runs/.../facts.jsonl` | **MISSING** | Metrics below **RECONSTRUCTED** from prior live forensics |

**M9 resolution (2026-06-30):** Two missing runs use **RECONSTRUCTED** metrics (Option C) in `baseline_reconstructed_controls.json`. One run **VERIFIED** (`1782742788`). Full before/after report: [baseline_vs_ws_primary_report.md](baseline_vs_ws_primary_report.md). Machine comparison: `var/reporting/m9/phase2_comparison.json`.

---

## Control 1 — success (`paired_binary_live_1782741234`)

**Description:** Full cycle — NO stop-loss, YES survivor take-profit, authoritative cashflow PnL.

| Metric | Value | Source |
|--------|-------|--------|
| `book_age_ms` at activation | **Not recorded in latency fact**; REST poll backbone → expect multi-second at arm (same class as bad_stop ~4.7s) | RECONSTRUCTED / inferred |
| `book_age_ms` at stop trigger | **Not in facts** | UNAVAILABLE |
| `book_age_ms` at exit planning | **Not in exit_submit facts** | UNAVAILABLE |
| FAK reject count (exits) | **0** | RECONSTRUCTED |
| Trigger bid vs exit avg (NO SL) | trigger **0.3841** → fill avg **0.41** (better than trigger) | RECONSTRUCTED |
| Trigger bid vs exit avg (YES TP) | trigger **0.8171** → fill avg **0.86** | RECONSTRUCTED |
| Expected vs actual slippage | Planned net **+$1.0605**; realized **+$1.30** (+$0.24 vs plan) | RECONSTRUCTED |
| `trigger_to_submit_ms` | **null** (latency sample activation-only) | UNAVAILABLE |
| `submit_to_ack_ms` | **null** | UNAVAILABLE |
| `trigger_to_fill_ms` | **null** | UNAVAILABLE |
| Cashflow PnL | **+$1.30** total (**+$0.26**/pair); buy **$5.05**, sell **$6.35** | RECONSTRUCTED (`paired_binary_realized_pnl`) |
| Activation abort reason | N/A — pair committed | — |

**Lifecycle (RECONSTRUCTED):** Entry YES @ 0.54, NO @ 0.47 → monitor ~76s → NO SL @ 0.41 → YES TP repriced 0.8171 → fill @ 0.86 → `DONE`.

---

## Control 2 — bad stop (`paired_binary_live_1782742788`) — VERIFIED

**Description:** YES stop-loss 6s after arm; 2 FAK rejects then fill @ 0.33; NO survivor never TP; `max_runtime_s` shutdown; PnL unavailable.

| Metric | Value | Source |
|--------|-------|--------|
| `book_age_ms` at activation | **4796** (YES), **4696** (NO); max **4796** | `paired_binary_book_capture_quality` |
| `book_age_ms` at stop trigger | **Not emitted** on `stop_plan` / `leg_stop` / `exit_submit_attempt` | UNAVAILABLE |
| `book_age_ms` at exit planning | **null** on all 3 `exit_submit_attempt` rows | UNAVAILABLE |
| FAK reject count (YES exit) | **2** (`oms_reject` FAK no-match) | facts.jsonl |
| Trigger bid vs exit avg (YES SL) | trigger bid **0.37** (≤ trigger **0.4641**) → fill avg **0.33** ($1.65/5) | facts + exit_lifecycle |
| Expected vs actual slippage | Est. loss at bid **$0.18**/pair; realized loser loss **0.22**/share (0.55−0.33) | `stop_plan` + OMS |
| `trigger_to_submit_ms` | **null** (no stop latency sample) | UNAVAILABLE |
| `submit_to_ack_ms` | **null** | UNAVAILABLE |
| `trigger_to_fill_ms` | **null** | UNAVAILABLE |
| Cashflow PnL | **`paired_binary_realized_pnl_unavailable`** — missing `no_exit_cash`, `no_exit_qty` | facts.jsonl |
| Partial recorded cashflow | YES entry −$2.75, NO entry −$2.30, YES exit +$1.65 → net **−$3.40** incomplete | OMS match evidence |
| Activation abort reason | N/A | — |
| End reason | `max_runtime_s` (~1202 ticks); NO bid ~0.20 vs TP **0.8971** | health + done facts |

**Latency sample (activation only):**

```json
{"event": "activation", "book_age_ms": 4796, "trigger_to_submit_ms": null, "submit_to_ack_ms": null}
```

**Phase 2 signal:** ~4.8s book age at activation under 5s REST poll; stop fired on touch bid from same stale backbone; FAK rejects then worse fill.

---

## Control 3 — activation abort (`paired_binary_live_1782737121`)

**Description:** YES FAK matched; NO FAK venue-rejected (balance/allowance); emergency unwind; ~17s total; no monitor phase.

| Metric | Value | Source |
|--------|-------|--------|
| `book_age_ms` at activation | **Not extracted** (facts missing) | UNAVAILABLE |
| `book_age_ms` at stop/exit | N/A — no pair monitor | — |
| FAK reject count | **1** entry (NO buy); unwind sells succeeded on attempt 14 | RECONSTRUCTED |
| Trigger vs exit | N/A (abort unwind only): BUY YES $2.55, SELL YES $2.60 → **+$0.05** round-trip | RECONSTRUCTED |
| Latency chain | **Not emitted** (no activation latency sample in abort path) | UNAVAILABLE |
| Cashflow PnL | **N/A** — pair never completed; no `paired_binary_realized_pnl` | RECONSTRUCTED |
| Activation abort reason | **`oms_reject`**: `not enough balance / allowance` on NO leg after YES matched (~$4.92 balance vs ~$2.59 needed incl. fees) | RECONSTRUCTED |
| Final state | **`FAILED`** / `PAIR_ABORTED_FLAT` | RECONSTRUCTED |

**Note:** Abort is primarily a **capital sequencing** issue (stale wallet vs venue after leg-1 spend), not book freshness — still relevant for M9 comparison but orthogonal to WS backbone.

---

## Cross-run baseline observations (REST/poll era)

1. **Activation book age:** Verified **~4.8s** on bad_stop; success run same backbone → same class of staleness at arm.
2. **Stop/exit book age:** Not logged at decision time — M7 gap.
3. **Touch-only triggers:** Monitor uses `read_leg_book` best bid; no depth-at-size at trigger.
4. **FAK exit quality:** Success run clean; bad_stop **2 rejects + 0.33 fill vs 0.37 trigger bid**.
5. **Latency facts:** Only activation `paired_binary_latency_sample`; stop/exit chain null.
6. **PnL:** Success authoritative; bad_stop unavailable; abort N/A.

---

## M9 extraction (2026-06-30)

```bash
python scripts/extract_paired_binary_metrics.py var/reporting/runs/paired_binary_live_1782742788
python scripts/compare_phase2_before_after.py --json-out var/reporting/m9/phase2_comparison.json
```

| Run | Extractor provenance | activation p95 (ms) | planner facts | latency_chain |
|-----|---------------------|---------------------|---------------|---------------|
| `1782741234` | RECONSTRUCTED | ~4700 | 0 | 0 |
| `1782742788` | VERIFIED | **4791** | 0 | 0 |
| `1782737121` | RECONSTRUCTED | UNAVAILABLE | 0 | 0 |
| `m8_validation_002c` | VERIFIED | **0.9** | 4 | 7 |

## Pre-M9 restore checklist (historical)

- [x] Re-parse verified control + treatment with `extract_paired_binary_metrics.py`
- [x] Document RECONSTRUCTED controls (Option C) — not silent substitution
- [x] Publish `baseline_vs_ws_primary_report.md`
- [ ] Optional: Option B substituted REST re-runs (`m9_control_rest_*_001`) to replace RECONSTRUCTED rows
