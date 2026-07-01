# Group C Implementation Summary (C0 / C1 — M7 wiring + M6 scheduler)

**Completed:** 2026-06-29  
**Scope:** Close M7 observability gaps on paired-binary material paths; hybrid event scheduler with debounced wake.

## Delivered

| Track | Work | Tests |
|-------|------|-------|
| **C0 — M7 wiring** | `strategies/paired_binary/observability.py`; monitor stop/TP/FAK-retry snapshots; activation + entry eval in `paired_binary_run.py`; pipeline reuse of `decision_id` / skip duplicate snapshot | `test_paired_binary_decision_snapshot_wiring.py`, `test_paired_binary_activation_latency_facts.py`, `test_paired_binary_stop_tp_decision_facts.py` |
| **C1 — M6 scheduler** | `runtime/market_update_coordinator.py`; hybrid wait loop + tick lock in `paired_binary_run.py`; authoritative store callback in `market_store.py`; user fill / sellability wake in `user_stream.py` + `allocation_runtime.py` | `test_market_update_coordinator.py`, `test_paired_binary_event_wake.py`, `test_tick_non_reentrancy.py` |

## C0 — Material decision observability

Paths now emit `decision_snapshot` (and `latency_chain` where applicable):

| Path | Emitted before / with planning |
|------|--------------------------------|
| Entry evaluation | `decision_snapshot` (`entry_eval`) |
| Activation | `decision_snapshot` + `latency_chain` |
| Stop trigger | `decision_snapshot` before exit dispatch |
| Take-profit trigger | `decision_snapshot` before exit dispatch |
| Exit submit | `execution_planner_evidence` linked via shared `decision_id` |
| FAK retry | New `decision_id`, snapshot, quality report, executable view, planner evidence |

Legacy paired-binary facts (`paired_binary_stop_plan`, `paired_binary_exit_submit_attempt`, etc.) remain; new Group B facts are additive.

**DataQualityGate:** still `observe_only` by default — verdicts emitted, decisions not blocked.

## C1 — Hybrid event scheduler

```yaml
runtime:
  paired_binary:
    poll_interval_s: 1.0
    max_decision_rate_per_market_ms: 75
```

**Wake sources (authoritative only):**

- `MarketStateStore.apply_*` on `coord.market_state` → `MarketUpdateCoordinator.notify_token_update`
- User WS TRADE → `notify_coordinator_user_fill`
- Allocation buy fill → `notify_coordinator_sellability`

**Loop behavior:**

- `async with coordinator.tick_lock` — no concurrent ticks per pair
- `wait_for_update(token_ids, timeout_s=poll_interval)` → `event_wake` or `timer`
- Debounce coalesces bursts; always evaluates latest store state
- Shadow store updates are ignored (store id not registered as authoritative)

**New facts:**

- `paired_binary_tick_source` — `event_wake` | `timer`
- `decision_coalesce_count` — coalesced notifications per wake

## Runtime posture (unchanged constraints)

- **REST authoritative** for live decisions; REST poll + bootstrap unchanged until M8.
- **WS shadow-only** for market books; shadow store does not wake paired-binary ticks.
- **Strategy logic / params unchanged** — thresholds, TP/SL, survivor, market selection untouched.
- **M8 not started** — no WS-primary cutover, no quality enforcement on decisions.

## Test results

54 Group C + regression tests passed locally:

```text
tests/test_paired_binary_decision_snapshot_wiring.py
tests/test_paired_binary_activation_latency_facts.py
tests/test_paired_binary_stop_tp_decision_facts.py
tests/test_market_update_coordinator.py
tests/test_paired_binary_event_wake.py
tests/test_tick_non_reentrancy.py
tests/test_paired_binary_monitor.py
tests/test_paired_binary_runtime.py
tests/test_data_quality_gate.py
tests/test_execution_planner.py
```

## Remaining blockers before M8 WS-primary cutover

1. **M8 cutover design** — flip authoritative store to WS_PRIMARY; gate `enforce` mode; disable REST poll when WS healthy.
2. **Live validation** — ≥1 live/shadow run with complete `latency_chain` on stop exit (ack/fill paths).
3. **M9 baseline artifacts** — 2/3 control `facts.jsonl` still missing for before/after comparison.
4. **Readiness tracker wiring** — `market_readiness_transition` facts on production startup path.
5. **Synthetic latency benchmark** — optional M9 script comparing poll-only vs event-wake stop-plan latency under live load.

## Not implemented (by design)

- M8 WS-primary cutover
- M9 baseline rerun / report
- BTC/RTDS, OBI, microprice, volatility, aggressor flow, ML, single-leg A/B, new survivor logic
