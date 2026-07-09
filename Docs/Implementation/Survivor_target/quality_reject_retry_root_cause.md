# Quality-reject survival exit retry — root cause

## Incident (Phase 1 trailing-enforce Run 1)

Sequence observed:

1. Trailing stop triggered (`survivor_trailing_stop_triggered`)
2. `survival_enforce_exit_requested` emitted
3. `survival_enforce_exit_skipped` with `skip_reason=quality_reject`
4. No OMS exit submitted
5. Survivor remained open until pre-close flatten

Run 2 (clean success) followed the same path through trailing trigger but passed quality gates and submitted FAK → matched → DONE.

This is **not** a trailing logic failure. It is an execution gating / lifecycle issue.

---

## Where `quality_reject` is generated

Primary path: `SurvivalExitPlanner.evaluate_exit()` in `src/tyrex_pm/survival/exit_planning.py`.

When `require_executable_evidence` is true and `DataQualityGate.allows_decision(report, decision_context)` returns false, the planner returns:

```python
verdict="defer", reason="quality_reject"
```

The gate report is built in `src/tyrex_pm/market_data/quality.py` via `DataQualityGate.evaluate_snapshot()`.

### Failure categories (diagnostic mapping)

| Category | Typical report reasons |
|----------|------------------------|
| Freshness | `book_age_reject`, `book_age_emergency`, `missing_snapshot`, `missing_bid_or_ask` |
| Spread | `spread_exceeds_max`, `missing_spread` |
| Depth | `insufficient_depth` |
| Sequence gap | `reconnect_gap` |

Run 1 context: a WS `reconnect_gap` was active (`ws_sequence_gap_detected`, market paused). Advisory exit evaluation uses `DecisionContext.TAKE_PROFIT`, which **rejects** on `reconnect_gap`. Trailing trigger logic can still fire using touch-bid fallback, but enforce dispatch requires a non-defer exit evaluation.

---

## Why the survival exit intent was not retried (before this patch)

1. **Two evaluation layers**: trailing FSM can trigger on touch bid while enforce dispatch requires `exit_eval.verdict` not in `{defer, blocked}`.

2. **Dead-end skip path**: In `monitor._maybe_dispatch_survival_enforcement()`, on quality reject the monitor emitted `survival_enforce_exit_skipped` and returned `[]` with no state latch.

3. **Trailing FSM one-shot**: `should_exit` is set only on ARMED→TRIGGERED transition. After TRIGGERED, if price recovers above trail floor, advisory does not re-assert enforce — so no second dispatch attempt on later ticks.

4. **Existing FAK retry is post-submit only**: `_retry_pending_exits()` handles OMS submitted → FAK rejected via `prepare_survival_enforce_trigger()` and `TP_PENDING_*` phase. Quality reject never reached that path because OMS was never called.

---

## How this differs from OMS FAK reject retry

| Aspect | OMS FAK reject retry | Quality reject (pre-submit) retry |
|--------|----------------------|-----------------------------------|
| Trigger | Order submitted, venue rejects FAK | Planner/gate rejects before OMS |
| State | `pending_trigger_type`, `TP_PENDING_*` phase | `pending_survival_exit_intent` latch |
| Re-eval context | Same order policy repricing | Fresh exit planning on WS tick |
| Backoff | Order policy attempt counters | `quality_reject_retry_backoff_s` on book updates |

---

## Fix (this patch)

When enforce skip reason is `quality_reject` and `survival.enforcement.retry_quality_rejects` is enabled:

1. Latch `pending_survival_exit_intent` on survivor leg state
2. On each WS monitor tick, re-evaluate exit planning with `DecisionContext.URGENT_EXIT` after backoff
3. Submit when quality passes; abandon on max attempts/time or pre-close flatten preempt
4. Emit retry lifecycle facts for validator tuning

Quality gates themselves are **not** weakened — only the retry lifecycle is added.
