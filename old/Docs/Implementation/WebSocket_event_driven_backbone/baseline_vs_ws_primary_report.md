# M9 — Baseline vs WS-primary before/after report

**Report date:** 2026-06-30  
**Phase 2 gate:** Milestone 9 — infrastructure comparison (not profitability)  
**Comparison JSON:** `var/reporting/m9/phase2_comparison.json`

> **Disclaimer:** This report measures infrastructure and execution evidence improvement. It does **NOT** establish positive expected value, stable edge, or strategy profitability.

---

## 1. Executive summary

**Infrastructure hypothesis:** **Supported** on verified evidence.

Under WS-primary (post-M8), the same paired-binary strategy (`cf2b1b7c2dbe15d9`) runs with:

- **~50× lower activation book age** (verified control ~4.8s → treatment canonical run ~1ms strict activation)
- **Sub-100ms decision snapshots** at entry/exit on the M8-validated run (`m8_validation_002c`)
- **Complete FAK planner evidence** and **latency_chain** on matched OMS (control era: 0 planner facts, 0 latency chains on verified bad_stop)
- **Zero REST-sourced OMS submits** on all three treatment runs

**Confidence caveats:**

- Two of three M0 control runs lack on-disk `facts.jsonl`; metrics are **RECONSTRUCTED** from M0 forensics (Option C — not silent substitution).
- Treatment runs use M8 validation scenario `dbc775ed4f4a2a20`, not the M0 control scenario `628d30b601a95627` — same strategy params, different market overlay and runtime caps.
- Treatment lifecycles ended on `max_runtime_s` (survivor timeout) rather than natural TP/SL outcomes seen in some controls.

**Verdict on stale data vs strategy:** Losses and slippage in the REST era were **materially influenced by multi-second book staleness and missing execution evidence**. WS-primary removes that infrastructure handicap; it does **not** prove the touch-only TP/SL logic has edge.

---

## 2. Control/treatment dataset and limitations

### Control set (REST/poll era)

| Run name | Run ID | Provenance | facts.jsonl | Role |
|----------|--------|------------|-------------|------|
| `paired_binary_live_1782741234` | (M0 forensics) | **RECONSTRUCTED** | **MISSING** | Success lifecycle (+$1.30 PnL forensics) |
| `paired_binary_live_1782742788` | `74965d05-369e-4497-a87b-a0f565bdff52` | **VERIFIED** | present (79 facts) | Bad stop + 2 FAK rejects |
| `paired_binary_live_1782737121` | (M0 forensics) | **RECONSTRUCTED** | **MISSING** | Activation abort (OMS balance) |

**Restore attempt:** Option A failed — no git history or archive for missing directories. Option B (substituted REST re-runs) was **not executed**; report uses Option C with explicit labeling.

### Treatment set (WS-primary, post-M8)

| Run name | Run ID | Provenance | M8 analyzer | Notes |
|----------|--------|------------|-------------|-------|
| `m8_validation_002` | `c8081d7c-5ed9-4277-b759-b995cd7b4727` | VERIFIED | partial lifecycle | No entry latency chain; short run |
| `m8_validation_002b` | `6faabd4c-9545-4f14-9348-6d5fc3c8d03a` | VERIFIED | pre-E4 stale activation | **Not canonical** — 1760ms activation bug |
| `m8_validation_002c` | `04056d08-b3b2-475b-a7cf-6cbb0138a3b0` | VERIFIED | **M8_VALIDATED** | **Primary treatment reference** |

All treatment runs preserve WS-primary posture:

```text
websocket.primary_enabled: true
rest.poll_enabled: false
quality.enforcement_mode: enforce
require_ws_primary_for_entry: true
allow_rest_recovery_for_entry: false
```

**Not run:** `m9_treatment_ws_primary_001/002/003` — existing M8 validation lifecycles satisfy ≥3 treatment requirement with documented scenario difference.

---

## 3. Frozen metadata block

| Field | Control (M0) | Treatment (M8 validation) |
|-------|--------------|---------------------------|
| Strategy YAML | `config/strategies/paired_binary.yaml` | same |
| Strategy SHA256 prefix | `cf2b1b7c2dbe15d9` | `cf2b1b7c2dbe15d9` ✓ |
| Scenario YAML | `live_paired_binary_tiny.yaml` (`628d30b601a95627`) | `m8_ws_primary_validation_002.yaml` (`dbc775ed4f4a2a20`) |
| `position_size` | 5 | 5 |
| `pair_stop_loss_pct` | 0.09 | 0.09 (unchanged) |
| `pair_take_profit_pct` | 0.30 | 0.30 (unchanged) |
| Order style | FAK entry/exit | FAK entry/exit |
| Git (treatment) | — | `ac5191d76aa26eea0c93cf8f004c0ba3e6cdf44e` |
| Market (verified control) | `shadow_test_market` | `m8_ws_primary_validation_002` |

Strategy logic, TP/SL, survivor logic, sizing, and signal set were **not modified** for M9.

---

## 4. Freshness comparison

### Activation book age (ms) — primary REST vs WS signal

| Run | Provenance | activation p50 | activation p95 | Backbone |
|-----|------------|----------------|----------------|----------|
| `1782741234` | RECONSTRUCTED | ~4700 (inferred) | ~4700 | REST_POLL |
| `1782742788` | VERIFIED | 4746 | **4791** | REST_POLL |
| `1782737121` | RECONSTRUCTED | UNAVAILABLE | UNAVAILABLE | REST_POLL |
| `m8_validation_002c` | VERIFIED | **0** | **0.9** | WS_PRIMARY |
| `m8_validation_002b` | VERIFIED | 1760 | 1760 | WS_PRIMARY (pre-E4 bug) |
| `m8_validation_002` | VERIFIED | 96 | 96 | WS_PRIMARY |

**Verified pair:** control bad_stop activation p95 **4791 ms** → treatment 002c **0.9 ms** (~5300× improvement).

### Decision snapshot book age (strict categories, treatment 002c)

| Category | p50 | p95 | p99 | samples |
|----------|-----|-----|-----|---------|
| All material | 89 | **92.2** | 92.8 | 5 |
| Entry | 81.5 | 91.9 | — | 2 |
| Activation | 0 | 0.9 | — | 3 |
| Exit (timeout) | 89 | 89 | — | 2 |

Control era: **no `decision_snapshot` facts** on verified bad_stop (0 samples); activation captured only via `paired_binary_book_capture_quality`.

---

## 5. Latency comparison

| Run | trigger→submit non_null | submit→ack non_null | trigger→fill non_null | latency_chain facts |
|-----|-------------------------|---------------------|----------------------|---------------------|
| Control success (recon) | 0 | 0 | 0 | 0 |
| Control bad_stop (verified) | 0 | 0 | 0 | 0 |
| Control abort (recon) | 0 | 0 | 0 | 0 |
| `m8_validation_002` | 0 | 0 | 0 | 3 |
| `m8_validation_002b` | 4 (p95 1344ms) | 4 | 4 | 5 |
| `m8_validation_002c` | 4 (p95 1286ms) | 4 (p95 0ms ack) | 4 (p95 1286ms) | 7 |

**Observability improved:** Treatment runs emit full `latency_chain` on matched OMS; control era had activation-only samples with null submit/ack/fill fields.

Venue ack latency (`submit_to_ack_ms`) is ~0ms on treatment (immediate ack path); trigger→fill dominated by venue matching (~390ms p50 on 002c).

---

## 6. Execution comparison

| Run | OMS submits | OMS matched | FAK rejects | Planner evidence | Incomplete planner |
|-----|-------------|-------------|-------------|------------------|---------------------|
| `1782742788` (control) | 3 | 3 | **2** | 0 | 0 |
| `1782741234` (recon) | — | — | 0 | 0 | — |
| `1782737121` (recon) | — | — | 1 (entry) | 0 | — |
| `m8_validation_002c` | 4 | 4 | **0** | **4** | 0 |
| `m8_validation_002b` | 4 | 4 | 0 | 4 | 0 |
| `m8_validation_002` | 2 | 2 | 0 | 2 | 0 |

Control bad_stop: YES SL triggered on ~4.8s stale book → 2 FAK rejects → fill @ 0.33 vs trigger bid 0.37.

Treatment 002c: timeout exits on **89ms** fresh snapshots; 0 FAK rejects; planner fields complete (`worst_price_to_fill`, `sweep_vwap`, `available_depth`, `snapshot_id`, `decision_id`).

---

## 7. FAK rejects / planner evidence comparison

| Metric | Control median | Treatment median | Improved? |
|--------|----------------|------------------|-----------|
| FAK reject count | 1 | 0 | Yes |
| Planner evidence count | 0 | 4 | Yes (observability) |
| Planner incomplete | 0 | 0 | — |

---

## 8. REST-source safety comparison

| Run | REST-sourced OMS submits | Steady-state book source |
|-----|--------------------------|--------------------------|
| Control bad_stop | 0 (book_source not tagged REST on submits) | REST poll backbone (~5s) |
| Treatment 002c | **0** | `websocket` (4/5 snapshots; 1 bootstrap) |

M8 criterion 4 confirmed: zero REST-sourced new entries on treatment. `rest_poll_disabled` + `ws_primary_cutover` facts present.

---

## 9. Lifecycle outcomes

| Run | Outcome | Survivor timeout | Abort reason |
|-----|---------|-------------------|--------------|
| `1782741234` | DONE | no | — |
| `1782742788` | DONE | no (max_runtime shutdown) | — |
| `1782737121` | FAILED | no | OMS balance/allowance |
| `m8_validation_002c` | DONE | yes (`max_runtime_s`) | — |
| `m8_validation_002b` | DONE | yes | — |
| `m8_validation_002` | DONE | yes | — |

Treatment runs completed paired-binary lifecycle (entry → activation → monitor → timeout exit) under WS-primary gates. Natural TP/SL outcomes were not observed on treatment set (timeout-driven exits).

---

## 10. Cashflow PnL comparison (strong caveat)

| Run | Realized PnL | Source |
|-----|--------------|--------|
| `1782741234` | **+$1.30** | RECONSTRUCTED forensics |
| `1782742788` | UNAVAILABLE | `paired_binary_realized_pnl_unavailable` (incomplete NO leg) |
| `1782737121` | N/A | pair never completed |
| Treatment runs | UNAVAILABLE / not authoritative | `paired_binary_realized_pnl` fact present but no comparable cashflow total |

**Do not interpret** treatment PnL as strategy validation. Different markets, scenarios, and exit paths (timeout vs TP/SL). M9 explicitly excludes profitability claims.

---

## 11. Answer: strategy bad or slowed by stale data?

**Primary answer:** The strategy was **slowed and misled by stale REST-poll books** at material decision points (verified ~4.8s activation age). WS-primary removes that handicap and adds execution evidence needed to diagnose FAK/slippage.

**Secondary answer:** Touch-only TP/SL with FAK exits still carries **strategy-level risk** (FAK rejects on bad_stop, capital sequencing on abort). Infrastructure fixes do not validate edge.

Stale data was a **confirmed contributor**; strategy logic remains **unproven for positive EV**.

---

## 12. Recommendation

| Question | Answer |
|----------|--------|
| Book freshness improved? | **Yes** (verified 4791ms → <1ms activation on 002c) |
| Latency observability improved? | **Yes** (latency_chain + planner on treatment) |
| Execution evidence improved? | **Yes** |
| REST-sourced entry risk eliminated? | **Yes** (0 REST OMS on treatment) |
| Planner evidence improved? | **Yes** |
| Strategy profitable / ready to scale? | **No conclusion** |

**M9 status:** `PHASE_2_COMPLETE` — infrastructure objectives met with documented control limitations.

**Next phase:** Strategy research (M10 hooks — BTC/RTDS, OBI, microprice) may begin **after** team sign-off on this report. Optional follow-up: Option B substituted REST control re-runs to replace RECONSTRUCTED controls for stronger quantitative baseline.

---

## Appendix — extraction tooling

```bash
python scripts/extract_paired_binary_metrics.py var/reporting/runs/paired_binary_live_1782742788
python scripts/compare_phase2_before_after.py --json-out var/reporting/m9/phase2_comparison.json
```

Reconstructed control definitions: `baseline_reconstructed_controls.json`
