# Group F — M9 baseline restoration + before/after report

**Date:** 2026-06-30  
**Status:** `PHASE_2_COMPLETE` (with documented control limitations)

---

## Step 1 — Baseline artifact restore

| Run ID | Restore result | Provenance |
|--------|--------------|------------|
| `paired_binary_live_1782741234` | **NOT RESTORED** — no archive/git | **RECONSTRUCTED** (Option C) |
| `paired_binary_live_1782742788` | **VERIFIED** on disk | **VERIFIED** |
| `paired_binary_live_1782737121` | **NOT RESTORED** — no archive/git | **RECONSTRUCTED** (Option C) |

Option A (exact archive restore) failed. Option B (substituted REST re-runs `m9_control_rest_*_001`) was **deferred** — report uses Option C with explicit labeling in `baseline_reconstructed_controls.json`.

---

## Step 2 — Metric extractor

| Script | Status |
|--------|--------|
| `scripts/extract_paired_binary_metrics.py` | Created — tolerant parsing, `UNAVAILABLE` for missing fields |
| `scripts/compare_phase2_before_after.py` | Created — loads verified + reconstructed controls |
| `tests/test_extract_paired_binary_metrics.py` | Created — fixture + corrupt-line tolerance |

---

## Step 3 — Control/treatment sets

### Control (3 runs, mixed provenance)

```text
paired_binary_live_1782741234  RECONSTRUCTED
paired_binary_live_1782742788  VERIFIED
paired_binary_live_1782737121  RECONSTRUCTED
```

### Treatment (3 WS-primary runs)

```text
m8_validation_002   / c8081d7c-5ed9-4277-b759-b995cd7b4727
m8_validation_002b  / 6faabd4c-9545-4f14-9348-6d5fc3c8d03a  (pre-E4; not canonical)
m8_validation_002c  / 04056d08-b3b2-475b-a7cf-6cbb0138a3b0  (M8_VALIDATED; primary reference)
```

`m8_validation_002c` included: strategy hash unchanged, facts complete, M8 analyzer passed, scenario hash recorded.

Additional `m9_treatment_ws_primary_001/002/003` runs **not required** — three post-M8 lifecycles already on disk.

---

## Step 4 — Treatment collection

Artifacts per treatment run under `var/reporting/runs/<name>/`:

- `facts.jsonl`, `manifest.json`, `run_summary.json` (where present)
- M8 analyzer output on 002c (`m8_validation_summary.json` if generated)
- WS-primary config posture verified on all three

---

## Step 5 — Report

Published: [baseline_vs_ws_primary_report.md](baseline_vs_ws_primary_report.md)

Machine-readable comparison: `var/reporting/m9/phase2_comparison.json`

---

## Step 6 — M9 conclusions (allowed)

- Book freshness improved ✓
- Latency observability improved ✓
- Execution evidence improved ✓
- REST-sourced entry risk eliminated ✓
- Planner evidence improved ✓

**Not concluded:** profitability, positive EV, capital scaling readiness.

---

## Step 7 — Documentation updated

- [baseline_runs.yaml](baseline_runs.yaml)
- [baseline_metrics.md](baseline_metrics.md)
- [audit_checklist.md](audit_checklist.md)
- [milestone_9_baseline_strategy_rerun_and_report.md](milestone_9_baseline_strategy_rerun_and_report.md)
- [phase_2.md](phase_2.md)
- [baseline_vs_ws_primary_report.md](baseline_vs_ws_primary_report.md) (new)
- [baseline_reconstructed_controls.json](baseline_reconstructed_controls.json) (new)

---

## Final recommendation

**`PHASE_2_COMPLETE`**

Optional hardening (not blocking): run Option B substituted REST controls to replace RECONSTRUCTED controls; rerun treatment on `live_paired_binary_tiny.yaml` for scenario-matched comparison.

**Post-M9 operational hardening (2026-06-29):** market-aware paired-binary state recovery, loop health facts, open-exposure force-flatten fixture validation — see [phase2_final_operational_hardening.md](phase2_final_operational_hardening.md).
