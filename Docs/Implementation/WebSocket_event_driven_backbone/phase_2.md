# Phase 2 — WebSocket-First, Event-Driven Market Data Backbone

**Status:** M8 validated; **M9 complete** — [baseline_vs_ws_primary_report.md](baseline_vs_ws_primary_report.md)  
**Program:** Infrastructure before strategy tuning  
**Milestones:** [milestone_0](milestone_0_audit_and_baseline.md) → [milestone_10](milestone_10_future_scalability_hooks.md)

---

## Executive summary

Tyrex_PM has a **mechanically mature** execution stack and a **partially implemented** market-data layer. Live order books are still populated primarily by **REST polling** (`market_data_rest_refresh_loop`, default 5s). Market WebSocket **parsers exist** but **no transport is wired**. Paired-binary runs the **same strategy logic** on stale touch prices; latency evidence is incomplete.

**Phase 2 is NOT about new strategy intelligence.** It is about running the **current paired-binary strategy unchanged** on a **WS-authoritative** backbone so we can measure whether losses and slippage were infrastructure problems or strategy-edge problems.

### Production target (non-negotiable)

```text
The final Phase 2 production target is WS-authoritative live market data.
Shadow mode (M1) is only a temporary validation step.
The project must NOT stop at shadow mode.
```

**Desired end state:**

```text
WebSocket is the authoritative live book source for strategy/risk/execution decisions.
REST is allowed for bootstrap, reconnect resync, reconciliation, and diagnostics.
REST is NOT allowed to open new paired-binary risk when WS is unavailable.
```

**Milestone roles:**

```text
M1  shadow mode     — validates WS payloads and freshness safely (REST still authoritative)
M8  WS-primary cutover — required production transition (NOT optional)
M9  baseline rerun  — measures whether the same strategy improves under WS-authoritative backbone
```

**Immediate success criterion (not profitability):**

```text
We can prove whether the current strategy runs faster and with better execution
evidence under WebSocket-authoritative market data.
```

**Safety invariants (unchanged):**

```text
Strategy → Intent only
RiskEngine mandatory
ExecutionPlanner mandatory
SingleWriterOMS sole writer
AllocationLedger + OrderStore unchanged in role
```

---

## What Phase 2 optimizes for

| Measure | Before (confirmed) | After (target) |
|---------|-------------------|----------------|
| Book freshness at decision | Often multi-second (REST poll) | WS-authoritative; age logged per decision |
| Stop/exit planning | Touch from stale snapshot | Executable depth at size |
| Latency chain | Partial (`LatencyTracker` at activation only) | trigger→submit→fill→sellable measurable |
| Decision evidence | Incomplete | snapshot_id, source, age, depth, quality verdict, features |
| Strategy logic | paired_binary as-is | **Same logic, new backbone** |

---

## First experiment after Phase 2 (Milestone 9)

Re-run paired-binary with **identical** parameters on the **WS-authoritative** backbone (post-M8). Compare before vs after:

```text
book_age_ms at entry / stop trigger / exit planning
trigger_to_submit_ms, submit_to_ack_ms, trigger_to_fill_ms
FAK reject rate
expected_slippage vs actual_slippage
activation failures due to stale data
stop slippage (trigger bid vs fill cash/qty)
survivor timeout behavior
cashflow PnL and variance
```

**Question to answer:**

```text
Was the strategy bad, or was it slowed down / misled by stale data?
```

**M9 can conclude:** whether speed, freshness, and execution evidence improved.  
**M9 cannot conclude:** that the strategy has positive expected value or stable edge.

Only after M9 should we add BTC/RTDS signals, OBI entries, microprice rules, ML, or new survivor logic. Those are documented in [milestone_10](milestone_10_future_scalability_hooks.md).

---

## Target architecture (summary)

```text
Polymarket Market WS ──┐
Polymarket User WS ────┤  (user WS already live)
                       ▼
              RawEvent Ingestion (M1 shadow → M8 authoritative)
                       ▼
              EventNormalizer (minimal contracts)
                       ▼
         MarketStateStore v2 + quality metadata
                       ▼
              DataQualityGate
                       ▼
         FeatureBuilder v0 (information prep only — no strategy change)
                       ▼
    Hybrid scheduler (event wake + timer ticks)
                       ▼
    Intent → RiskEngine → ExecutionPlanner (ExecutableBookView)
                       ▼
    validate_planned_order → SingleWriterOMS → Venue REST submit
```

See milestone docs for implementation detail.

---

## A. Verdict on each milestone (with refinements)

| # | Milestone | Verdict | Key refinement |
|---|-----------|---------|----------------|
| M0 | Audit & baseline | Agree | File-by-file TODO + **frozen baseline run IDs** |
| M1 | WS shadow ingestion | Agree strongly | **Separate `MarketStateStoreShadow`**; REST authoritative; temporary only |
| M2 | MarketStateStore v2 | Agree | Backward compat; capture APIs; event ordering fields |
| M3 | DataQualityGate | Agree strongly | **Exact v1 rules** for ENTRY/TP/STOP; no ambiguous EMERGENCY_ONLY |
| M4 | ExecutableBookView + planner | Agree strongly | Depth-at-size; **fresh snapshot on every FAK retry** |
| M5 | FeatureBuilder v0 | Agree (Option B) | Minimal book features only; **no strategy change** |
| M6 | Hybrid event scheduler | Agree | Debounce 75ms; non-reentrancy; user WS wake |
| M7 | Latency facts | Agree | Decision snapshots + latency chain |
| M8 | **WS-primary cutover** | **Required** | Production transition; strengthened acceptance criteria |
| M9 | Baseline rerun & report | Required gate | Same strategy; frozen config hashes; Phase 2 "done" |
| M10 | Future hooks | Docs only | BTC/RTDS, OBI, ML — not implemented |

---

## B. Strategic sequencing

```text
M0  Audit + frozen baseline runs
M1  WS shadow (MarketStateStoreShadow only — NOT production end state)
M2  Store v2 metadata + capture APIs
M3  DataQualityGate (exact v1 rules)
M4  ExecutableBookView + planner evidence + FAK retry policy
M5  FeatureBuilder v0
M6  Hybrid event scheduler
M7  Latency facts (decision snapshots)
M8  WS-primary cutover  ← required production transition (**Group D implemented; live validation pending**)
M9  Same-strategy rerun + before/after report  ← Phase 2 completion gate
M10 Future hooks (docs only)
```

**Deferred from Phase 2 implementation:** FeatureBuilder v1+ (OBI, microprice, volatility, BTC), raw-event full persistence, BTC/RTDS ingest, strategy logic changes, new entry signals, ML.

---

## REST policy (exact — all milestones)

| Source | Allowed use | Sufficient for TRADING_ENABLED? | New paired entry? | Stop/urgent exit? |
|--------|-------------|--------------------------------|-------------------|-------------------|
| `REST_BOOTSTRAP` | Startup seed | **No** | **No** | No (refresh context only) |
| `REST_RECOVERY` | Post-reconnect/gap resync | **No** | **No** | Risk reduction only if configured |
| `REST_POLL` | Legacy live poll | **No** | **No** | **Deprecated** in WS-primary mode |
| `WEBSOCKET` (WS_PRIMARY) | Live decisions | Required path | **Yes** (with PASS) | Preferred |

```yaml
runtime:
  market_data:
    quality:
      require_ws_primary_for_entry: true
      allow_rest_recovery_for_exit: true   # deployment choice: may set false for first prod run
      allow_rest_recovery_for_entry: false
```

**Deployment note:** Setting `allow_rest_recovery_for_exit: false` for the first production run is valid — semantics must still be explicit in code (REST recovery never creates or increases exposure).

**Rules:**

```text
New paired-binary entry:     WS_PRIMARY + DataQualityGate PASS
Take profit:                 WS_PRIMARY + DataQualityGate PASS
Stop / urgent exit:          prefer WS_PRIMARY with PASS or DEGRADED
                             if WS disconnected or gap: block entries, PAUSED/DEGRADED
                             REST recovery may refresh state for risk reduction only
                             REST recovery must never create or increase exposure
```

---

## DataQualityGate v1 rules (exact)

### Age thresholds (under `market_profiles.crypto_5m`)

```yaml
market_profiles:
  crypto_5m:
    pass_max_age_ms: 750
    reject_max_age_ms: 1500
    emergency_max_age_ms: 3000
```

**Age interpretation:**

```text
age <= pass_max_age_ms:
    PASS if all other checks pass

pass_max_age_ms < age <= reject_max_age_ms:
    DEGRADED for exits only; REJECT for entries and TP

reject_max_age_ms < age <= emergency_max_age_ms:
    EMERGENCY_ONLY for existing-risk exits only

age > emergency_max_age_ms:
    REJECT_DECISION; no automated decision except documented manual/emergency policy
```

### By decision context

**ENTRY:** PASS only. Requires WS_PRIMARY, both legs, age ≤ pass_max_age_ms, non-empty bid/ask, spread ok, depth ok, no reconnect gap.

**TAKE_PROFIT:** PASS only. Same as entry — TP is optional and must not execute from bad data.

**STOP / URGENT_EXIT:**

| Verdict | Behavior |
|---------|----------|
| PASS | Normal executable-depth planning |
| DEGRADED | Allowed for risk reduction; planner must set `quality_verdict=DEGRADED` in evidence |
| EMERGENCY_ONLY | Allowed **only** to reduce existing exposure; latest snapshot + explicit emergency reason + stricter evidence logging |
| REJECT_DECISION | No new risk; for open risk → REST_RECOVERY if configured, else `MANUAL_INTERVENTION_REQUIRED` |

---

## Event ordering and gap handling (deterministic)

Every WS connection tracks:

```text
connection_id
local_event_counter
received_monotonic_ns
```

**If payload provides sequence/hash:**

```text
store last_sequence/hash per token
if sequence <= last_sequence:
    ignore event; emit out_of_order_event fact
if gap detected:
    set reconnect_gap=true
    block entries
    trigger REST_RECOVERY resync
    wait for fresh WS book on both legs
    clear reconnect_gap only after DataQualityGate PASS
```

**If payload does NOT provide sequence/hash:**

```text
use received_monotonic_ns ordering locally
after any reconnect, always run REST_RECOVERY bootstrap
block entries until fresh WS book on both legs and DataQualityGate PASS
```

---

## FAK / urgent exit retry policy (M4)

```text
Every retry must capture a fresh MarketStateSnapshot.
Never reuse old planner evidence for a retry.
Each retry gets: new decision_id, snapshot_id, DataQualityReport,
                 ExecutableBookView, PlannerEvidence

Partial fill:
  remaining_qty = original_qty - filled_qty
  capture fresh snapshot → new ExecutableBookView(remaining_qty)
  re-run DataQualityGate → plan retry → new evidence

FAK reject:
  capture fresh snapshot
  do not reuse previous touch/worst_price/sweep_vwap
  re-evaluate quality → re-plan from current executable depth → new evidence
```

---

## Backpressure and non-reentrancy (M6)

```yaml
runtime:
  paired_binary:
    max_decision_rate_per_market_ms: 75   # initial crypto_5m value
```

```text
coalesce market updates during debounce window
always evaluate latest state
drop old decision opportunities, not old state
never run two paired_binary ticks concurrently for the same pair
wake on market WS updates and relevant user WS fill/sellability updates
timer still fires for timeouts and health
```

---

## Startup readiness states

```text
STARTING → REST_BOOTSTRAPPED → WS_CONNECTED → WS_BOOK_RECEIVED (per leg)
→ BOTH_LEGS_READY → QUALITY_PASS → TRADING_ENABLED
```

No live paired entry before `TRADING_ENABLED`. `REST_BOOTSTRAP` alone is never sufficient.

---

## Reconnect behavior

```text
on WS disconnect:
  block entries
  mark PAUSED/DEGRADED
  allow emergency exits via REST recovery if configured (risk reduction only)

on reconnect:
  REST_RECOVERY bootstrap both legs
  wait for fresh WS book on both legs
  clear reconnect_gap only after DataQualityGate PASS
  re-enter TRADING_ENABLED when readiness + quality pass
```

---

## Cross-cutting policies

### User WS + market WS synchronization (M7 latency facts)

```text
submit_to_ack_ms, ack_to_user_fill_ms, fill_to_sellable_ms
user_ws_age_ms, wallet_position_age_ms, market_book_age_ms
```

### Market-data health kill switch

Block **new entries** when: WS disconnected too long, reconnect count too high, p95 book_age_ms too high, quality repeatedly REJECT. Exits may proceed under degraded policy.

### Market profiles

Phase 2 implements **`crypto_5m`** for paired-binary testing. See [milestone_3](milestone_3_data_quality_gate.md) for full profile structure.

---

## Confirmed current behavior (codebase)

| Area | Fact |
|------|------|
| Live books | REST poll via `market_data_rest_refresh_loop` |
| WS parsers | `ingestion/market_stream.py` — not wired |
| User WS | `ingestion/user_stream.py` — live |
| Store | `state/market_store.py` — `ts = utc_now()` at apply |
| Paired-binary | Poll loop; touch-only triggers |
| Planner | FAK uses `is_stale` + `estimate_fill_price` |

Full file inventory → [milestone_0](milestone_0_audit_and_baseline.md).

---

## Phase 2 success report template

```text
Before Phase 2:
  REST/polling book age often multi-second.
  Stop/exit could use stale top-of-book.
  Latency fields incomplete.

After Phase 2 (M8 cutover + M9 report):
  Market decisions use WS-authoritative snapshots.
  Every decision has decision_id, snapshot_id, source, age, depth, quality verdict.
  FeatureBuilder v0 fields present in decision_snapshot (no strategy change).
  Stop plans use executable depth at size; retries use fresh snapshots.
  trigger→submit→fill latency measurable.
  Before/after execution and PnL comparison documented (M9).
```

---

## Milestone index

| # | Document | Objective |
|---|----------|-----------|
| 0 | [milestone_0_audit_and_baseline.md](milestone_0_audit_and_baseline.md) | Audit + **frozen baseline run IDs** |
| 1 | [milestone_1_ws_shadow_ingestion.md](milestone_1_ws_shadow_ingestion.md) | Shadow WS → **`MarketStateStoreShadow` only** |
| 2 | [milestone_2_market_state_store_v2.md](milestone_2_market_state_store_v2.md) | Store v2 + event ordering fields |
| 3 | [milestone_3_data_quality_gate.md](milestone_3_data_quality_gate.md) | **Exact v1** quality rules |
| 4 | [milestone_4_executable_book_view_and_planner.md](milestone_4_executable_book_view_and_planner.md) | Depth planning + **FAK retry policy** |
| 5 | [milestone_5_feature_builder_v0.md](milestone_5_feature_builder_v0.md) | Minimal FeatureBuilder v0 |
| 6 | [milestone_6_hybrid_event_scheduler.md](milestone_6_hybrid_event_scheduler.md) | Event wake + debounce + non-reentrancy |
| 7 | [milestone_7_latency_facts_and_speed_assessment.md](milestone_7_latency_facts_and_speed_assessment.md) | Decision snapshots + latency chain |
| 8 | [milestone_8_ws_primary_cutover.md](milestone_8_ws_primary_cutover.md) | **Required** WS-primary production cutover |
| 9 | [milestone_9_baseline_strategy_rerun_and_report.md](milestone_9_baseline_strategy_rerun_and_report.md) | Same-strategy rerun + report (**Phase 2 done**) |
| 10 | [milestone_10_future_scalability_hooks.md](milestone_10_future_scalability_hooks.md) | Future signals (docs only) |

---

## Remaining open questions

| # | Question | Resolve in |
|---|----------|------------|
| 1 | Polymarket market WS URL, subscribe payload, event field names | M1 spike + fixture capture |
| 2 | Sequence/hash present in live WS payloads? | M1 live capture (policy defined either way) |
| 3 | `crypto_5m` profile binding: strategy YAML vs market metadata | M3 config |
| 4 | `allow_rest_recovery_for_exit: false` for first prod run? | Deployment choice before M8 |
| 5 | Store full book or cap at top N on apply? | M2 |
| 6 | `exchange_ts` field name in Polymarket payload | M1 fixture |

**Resolved (no longer open):** shadow store architecture (separate instance), EMERGENCY_ONLY semantics, FAK retry policy, event ordering rules, debounce default (75ms), milestone numbering.

---

## Related docs

- [phase_2_market_state_store.md](../architecture_enhance/phase_2_market_state_store.md)
- [phase_4_6_paired_binary_strategy_production_protection.md](../architecture_enhance/phase_4_6_paired_binary_strategy_production_protection.md)

---

*Last updated: 2026-06-30 — M9 before/after report published; Phase 2 infrastructure gate complete.*
