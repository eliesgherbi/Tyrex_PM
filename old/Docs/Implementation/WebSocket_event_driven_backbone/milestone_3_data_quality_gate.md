# Milestone 3 — DataQualityGate

## Objective

Implement a **central DataQualityGate** with **exact v1 rules** for ENTRY, TAKE_PROFIT, and STOP/URGENT_EXIT — no ambiguous `EMERGENCY_ONLY` behavior.

## Why this milestone exists

Stale REST books pass coarse `is_stale()`. Entries and TP require strict PASS on WS_PRIMARY data. Stops require graded exit paths without opening new risk.

## Current codebase state

**Confirmed:** `entry_eval.py` uses `read_leg_book` + `store.is_stale(max_age_s=5)`. `monitor.py` uses touch bid without quality verdict.

## Target behavior

### Exact REST policy (enforced by gate + runtime)

```text
REST_BOOTSTRAP:     startup seed only; NOT sufficient for TRADING_ENABLED
REST_RECOVERY:      post-reconnect resync; NOT sufficient for new entries
REST_POLL:          deprecated for live decisions; disabled at M8 in healthy state

New paired-binary entry:  WS_PRIMARY + PASS
Take profit:              WS_PRIMARY + PASS
Stop / urgent exit:       prefer WS_PRIMARY; PASS | DEGRADED | EMERGENCY_ONLY per age rules
REST recovery:            risk reduction only; never create or increase exposure
```

### Age thresholds (`market_profiles.crypto_5m`)

```yaml
runtime:
  market_data:
    quality:
      require_ws_primary_for_entry: true
      allow_rest_recovery_for_exit: true
      allow_rest_recovery_for_entry: false
      market_profile: crypto_5m
    market_profiles:
      crypto_5m:
        pass_max_age_ms: 750
        reject_max_age_ms: 1500
        emergency_max_age_ms: 3000
        max_spread: 0.15
        min_depth_at_size: 5
        require_external_price: false
```

**Age → verdict mapping (after source/leg/spread/depth checks):**

```text
age <= pass_max_age_ms:
    PASS (if all checks pass)

pass_max_age_ms < age <= reject_max_age_ms:
    DEGRADED — exits only
    REJECT_DECISION — entries and TP

reject_max_age_ms < age <= emergency_max_age_ms:
    EMERGENCY_ONLY — existing-risk exits only
    REJECT_DECISION — entries, TP, and new risk

age > emergency_max_age_ms:
    REJECT_DECISION for all automated decisions
```

### By decision context

**ENTRY — PASS only**

Requires: `source_quality == WS_PRIMARY`, both legs present, age ≤ pass_max_age_ms, non-empty bid/ask on entry sides, spread ≤ max_spread, depth ≥ min_depth_at_size, `reconnect_gap == false`.

**TAKE_PROFIT — PASS only**

Same requirements as ENTRY for the exit leg/pair. TP is optional and must not fire from degraded data.

**STOP / URGENT_EXIT**

| Verdict | Allowed action |
|---------|----------------|
| PASS | Normal executable-depth planning |
| DEGRADED | Risk reduction allowed; planner **must** set `quality_verdict=DEGRADED` in evidence |
| EMERGENCY_ONLY | Reduce **existing** exposure only; use latest snapshot; emit `emergency_reason`; stricter evidence logging |
| REJECT_DECISION | No new risk; if open position → trigger REST_RECOVERY if `allow_rest_recovery_for_exit` else `MANUAL_INTERVENTION_REQUIRED` |

**WS disconnected or `reconnect_gap`:** block entries; market PAUSED/DEGRADED; exits per STOP rules after optional REST_RECOVERY refresh.

### Startup readiness

```text
STARTING → REST_BOOTSTRAPPED → WS_CONNECTED → WS_BOOK_RECEIVED (each leg)
→ BOTH_LEGS_READY → QUALITY_PASS → TRADING_ENABLED
```

No paired entry before `TRADING_ENABLED`. `REST_BOOTSTRAP` alone never reaches `TRADING_ENABLED`.

### Health kill switch (block new entries)

```yaml
runtime:
  market_data:
    health:
      max_ws_disconnect_s: 30
      max_reconnects_per_hour: 10
      max_p95_book_age_ms: 2000
```

## Files likely touched

- `src/tyrex_pm/runtime/market_data_runtime.py` — readiness state machine
- `src/tyrex_pm/runtime/paired_binary_run.py` — gate before entry; readiness wait
- `src/tyrex_pm/strategies/paired_binary/entry_eval.py`
- `src/tyrex_pm/strategies/paired_binary/monitor.py` — exit contexts
- `src/tyrex_pm/runtime/config.py`
- `src/tyrex_pm/risk/engine.py`
- `src/tyrex_pm/runtime/health_runtime.py`

## New files likely created

- `src/tyrex_pm/market_data/quality.py`
- `src/tyrex_pm/market_data/readiness.py`
- `tests/test_data_quality_gate.py`
- `tests/test_market_readiness_states.py`
- `tests/test_quality_entry_vs_exit.py`
- `tests/test_emergency_only_semantics.py`

## Contracts / interfaces

```python
class DecisionContext(str, Enum):
    ENTRY = "entry"
    TAKE_PROFIT = "take_profit"
    STOP = "stop"
    URGENT_EXIT = "urgent_exit"

class QualityVerdict(str, Enum):
    PASS = "pass"
    DEGRADED = "degraded"
    EMERGENCY_ONLY = "emergency_only"
    REJECT_DECISION = "reject_decision"

@dataclass(frozen=True)
class DataQualityReport:
    verdict: QualityVerdict
    reasons: tuple[str, ...]
    book_age_ms: int | None
    source: BookSource | None
    source_quality: SourceQuality | None
    reconnect_gap: bool
    spread: Decimal | None
    depth_at_size: Decimal | None
    profile_id: str
    emergency_reason: str | None = None  # required when EMERGENCY_ONLY

class DataQualityGate:
    def evaluate_snapshot(self, snap, *, context: DecisionContext, size: Decimal) -> DataQualityReport: ...
    def evaluate_pair(self, pair, *, context: DecisionContext, size: Decimal) -> DataQualityReport: ...
```

## Facts / observability changes

- `data_quality_verdict` — context, verdict, reasons, profile_id, emergency_reason
- `market_readiness_transition`
- `market_data_health_block`
- `manual_intervention_required` — when REJECT_DECISION on open risk and recovery disabled

## Tests to add or update

- ENTRY at age 800ms → REJECT; STOP at 800ms → DEGRADED
- ENTRY at age 2000ms → REJECT; STOP at 2000ms → EMERGENCY_ONLY
- age 3500ms → REJECT for all contexts
- TP at DEGRADED → blocked
- Entry blocked on REST_BOOTSTRAP / REST_RECOVERY / REST_POLL when `require_ws_primary_for_entry`
- REST_RECOVERY exit allowed when configured; never increases exposure
- TRADING_ENABLED requires WS_PRIMARY PASS on both legs
- reconnect_gap blocks entry until cleared

## Acceptance criteria

- [x] All age-band tests pass per table above
- [x] ENTRY and TP require PASS only
- [x] EMERGENCY_ONLY never used for entry or TP
- [x] `crypto_5m` profile drives paired-binary scenario
- [x] Readiness state machine in facts (tracker + tests; runtime wiring at M8)

## Implementation status (Group B)

**Shipped:** `src/tyrex_pm/market_data/quality.py`, `readiness.py`; config `runtime.market_data.quality.enforcement_mode` defaults to **`observe_only`**. Tests cover **`enforce`** mode with exact M8 rules.

## Risks

- Over-strict gate in pre-M8 REST-only runs — gate may run in "observation mode" until M8; document flag if needed

## Open questions

- `allow_rest_recovery_for_exit: false` for first prod run — deployment choice before M8

## Definition of done

Gate integrated at entry, TP, and stop paths; exact v1 rules tested; readiness enforced; REST policy documented in code comments matching this spec.

## Not in scope

- FeatureBuilder v0 ([M5](milestone_5_feature_builder_v0.md))
- WS-primary cutover ([M8](milestone_8_ws_primary_cutover.md))
- BTC external price
