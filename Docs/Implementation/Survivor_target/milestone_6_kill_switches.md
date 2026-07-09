# Milestone 6 — Kill switches

**Program:** [phase1.md](phase1.md) §6 Phase 1.6  
**Status:** specification — not implemented  
**Depends on:** [Milestone 0](milestone_0_market_lifecycle.md) (stable flatten paths)

---

## 1. Purpose

Implement **KillSwitchManager** — loss and attempt counters that deny entry, pause strategy, or force-flatten pair exposure using existing reduce-only emergency paths. **No hard cancel-all** (Phase 6 out of scope).

---

## 2. Why this milestone exists

Portfolio-level damage control: repeated failures, daily loss limits, and manual intervention counts require automatic stops beyond per-trade survival logic.

---

## 3. Scope

- `survival/kill_switches.py`
- Hooks in `paired_binary_run.py` (pre-tick, pre-entry)
- Hook in `risk/engine.py` for deny (reuse `check_kill_switch` pattern)
- Optional persistence under `var/state/kill_switches/`
- Fact `kill_switch_triggered`

---

## 4. Non-goals

- Hard cancel-all / cancel every open order (Phase 6)
- Emergency liquidation of unrelated positions
- Removing Phase 2 reduce-only bypass

---

## 5. Current code areas to review

| File | Notes |
|------|-------|
| `risk/kill_switch.py` | Soft kill boolean |
| `risk/engine.py` | Gate #1 |
| `runtime/paired_binary_shutdown.py` | Force-flatten |
| `strategies/paired_binary/facts.py` | `emit_manual_intervention_required` |
| `runtime/health_runtime.py` | Health flags pattern |

---

## 6. Target architecture

```text
KillSwitchManager.check(ctx) → KillSwitchDecision
  triggered: bool
  switch_name: str
  action: deny_entry | pause_strategy | force_flatten_pair | hard_stop
```

Counters updated on: lifecycle terminal, realized PnL, no-entry summary, quality streak, manual intervention facts.

---

## 7. Detailed implementation plan

### 7.1 Files to create

| File | Purpose |
|------|---------|
| `src/tyrex_pm/survival/kill_switches.py` | Manager + counters |
| `tests/test_survival_kill_switches.py` | |

### 7.2 Files to modify

| File | Changes |
|------|---------|
| `runtime/paired_binary_run.py` | Pre-tick check; flatten on `force_flatten_pair` |
| `risk/engine.py` | Optional: consult manager for extended deny (or runtime-only deny before intent) |
| `runtime/config.py` | `survival.kill_switches` |
| `reporting/schema_v2.py` | `kill_switch_triggered` |

### 7.3 Switches (locked)

| Switch | Trigger | Action |
|--------|---------|--------|
| `per_pair_max_loss_usd` | Realized pair PnL < -threshold | deny entry; force-flatten if open |
| `daily_max_loss_usd` | Rolling daily sum | deny all entry |
| `max_failed_lifecycle_count` | FAILED terminal count | pause strategy |
| `max_consecutive_no_entry` | Loop ends without entry streak | pause entry |
| `max_bad_market_quality_streak` | Quality reject streak | pause entry |
| `max_manual_intervention_count` | Manual intervention facts | hard_stop until reset |

### 7.4 Types

```python
@dataclass(frozen=True)
class KillSwitchDecision:
    triggered: bool
    switch_name: str | None
    action: str | None
    current_value: Decimal | int | None
    threshold: Decimal | int | None

class KillSwitchManager:
    def __init__(self, cfg: KillSwitchConfig, state_path: Path | None): ...
    def record_lifecycle_terminal(self, phase: str, pnl: Decimal | None): ...
    def record_no_entry_run(self): ...
    def record_quality_reject(self): ...
    def record_manual_intervention(self): ...
    def check(self, *, owner_id: str, pair_id: str | None) -> KillSwitchDecision: ...
    def reset_daily_if_needed(self, now_ts: float): ...
```

### 7.5 Force-flatten

Call `handle_open_exposure_at_shutdown` with reason `kill_switch_<name>`. **Only after M0** flatten paths verified.

### 7.6 Config

```yaml
survival:
  kill_switches:
    per_pair_max_loss_usd: "0.50"
    daily_max_loss_usd: "5.00"
    max_failed_lifecycle_count: 3
    max_consecutive_no_entry: 10
    max_bad_market_quality_streak: 5
    max_manual_intervention_count: 2
    persist_daily: true
```

Kill switches active only when `survival.enabled: true` OR separate `kill_switches.enabled` flag (spec: gate behind survival for Phase 1 rollout).

---

## 8. Config changes

Add `KillSwitchConfig`; disabled when survival disabled.

---

## 9. State / data model changes

Optional JSON persistence:

```text
var/state/kill_switches/<owner_id>.json
  daily_loss, daily_date, consecutive_no_entry, failed_lifecycle_count, ...
```

---

## 10. Facts / observability

**`kill_switch_triggered`:** switch_name, threshold, current_value, action, pair_id, owner_id.

---

## 11. Tests

### `tests/test_survival_kill_switches.py`

- Per-pair loss triggers deny + flatten call (mock shutdown)
- Daily reset at UTC boundary
- Hard stop blocks loop continuation
- No cancel-all OMS calls

---

## 12. Acceptance criteria

- [ ] Threshold crossed → deny entry.
- [ ] Flatten uses existing reduce-only path.
- [ ] No hard cancel-all introduced.
- [ ] Does not force-flatten until M0 shutdown integration stable.

---

## 13. Risks and rollback plan

| Risk | Mitigation |
|------|------------|
| False daily stop on tiny accounts | Thresholds in scenario overlay only |
| Persist corruption | Version field + reset |

**Rollback:** disable kill_switches in config.

---

## 14. Dependencies and next milestone

**Depends on:** M0 flatten/shutdown stable.

**Next:** [Milestone 7](milestone_7_observability_validation_and_live_protocol.md)
