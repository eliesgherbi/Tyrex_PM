# Milestone 1 — Survival models and ledger-based target policy

**Program:** [phase1.md](phase1.md) §6 Phase 1.1  
**Status:** specification — not implemented  
**Depends on:** [Milestone 0](milestone_0_market_lifecycle.md) (recommended; can stub lifecycle clock for unit tests)

---

## 1. Purpose

Introduce the `survival/` package skeleton and **ledger-based** survivor target selection after loser exit. Replace price-delta-only repricing with cash/qty economics and classify impossible/unrealistic targets.

---

## 2. Why this milestone exists

Current code (`pnl.reprice_survivor_target_after_loser_exit`, `exit_engine.reprice_survivor_after_loser_exit`) computes:

```text
required = realized_loser_loss + desired_net_profit
planned_target = survivor_entry + required
```

This treats **full recovery as automatic** and ignores matched cashflows, fees, and binary price bounds. Live evidence shows survivors stall below ambitious repriced targets.

---

## 3. Scope

- Create `src/tyrex_pm/survival/` package
- `models.py`, `target_policy.py`
- Config parsing for `survival.enabled`, `survival.target_policy.*`
- Wire into loser-exit path when `survival.enabled: true`
- Facts for target selection/classification
- Unit tests

When `survival.enabled: false`, **no code path changes** — existing `reprice_survivor_target_after_loser_exit` remains.

---

## 4. Non-goals

- Reachability, stall, trailing stop (M3–M4)
- Executable exit adapter enforcement (M2) — may pass `executable_bid=None` initially
- Changing monitor TP trigger logic beyond storing new targets
- ML, external signals, capital scaling

---

## 5. Current code areas to review

| File | Symbols |
|------|---------|
| `strategies/paired_binary/pnl.py` | `reprice_survivor_target_after_loser_exit`, `SurvivorTargetPlan`, `desired_net_profit_per_pair` |
| `strategies/paired_binary/exit_engine.py` | `reprice_survivor_after_loser_exit` |
| `runtime/paired_binary_run.py` | `_update_post_exit_state`, loser fill handling, `emit_winner_target_repriced` |
| `strategies/paired_binary/state.py` | `PairedBinaryRuntimeState`, leg cash/qty fields |
| `strategies/paired_binary/facts.py` | `emit_winner_target_repriced` |
| `runtime/config.py` | Strategy config parsing |

---

## 6. Target architecture

```text
loser exit fill confirmed
  → if !survival.enabled: existing pnl.reprice_survivor_target_after_loser_exit
  → else:
       build SurvivorLegContext from state (ledger cash/qty)
       SurvivorTargetPolicy.select_plan(ctx) → SurvivorTargetPlan
       classify → IMPOSSIBLE | UNREALISTIC | VALID_CANDIDATE
       apply plan to state (yes_target / no_target / planned_target)
       emit survivor_target_selected (+ impossible/unreachable if applicable)
       initialize SurvivorLegState baseline for stall (M3)
```

---

## 7. Detailed implementation plan

### 7.1 Files to create

| File | Purpose |
|------|---------|
| `src/tyrex_pm/survival/__init__.py` | Export public types |
| `src/tyrex_pm/survival/models.py` | Shared dataclasses/enums |
| `src/tyrex_pm/survival/target_policy.py` | `SurvivorTargetPolicy` |
| `tests/test_survival_target_policy.py` | Unit tests |

### 7.2 Files to modify

| File | Changes |
|------|---------|
| `runtime/config.py` | `SurvivalConfig`, `SurvivalTargetPolicyConfig`; parse under `survival:` |
| `strategies/paired_binary/exit_engine.py` | Delegate repricing to survival when enabled |
| `runtime/paired_binary_run.py` | Pass survival config; ensure facts emitted |
| `strategies/paired_binary/state.py` | Optional: `survivor_leg_state: SurvivorLegState | None` field or parallel storage |
| `strategies/paired_binary/facts.py` | New emitters (or `survival/facts.py` re-exported) |
| `reporting/schema_v2.py` | New fact constants |

### 7.3 Types to add (`models.py`)

```python
class SurvivorTargetMode(str, Enum):
    FULL_RECOVERY = "full_recovery"
    BREAKEVEN = "breakeven"
    SMALL_PROFIT = "small_profit"
    SMALL_LOSS = "small_loss"
    DYNAMIC = "dynamic"

class SurvivorTargetClassification(str, Enum):
    IMPOSSIBLE = "impossible"       # required_price > 1.0
    UNREALISTIC = "unrealistic"     # > max_reasonable_exit_price
    VALID_CANDIDATE = "valid_candidate"

@dataclass(frozen=True)
class SurvivorLegContext:
    survivor_leg: Literal["yes", "no"]
    yes_entry_qty: Decimal
    no_entry_qty: Decimal
    yes_entry_cash: Decimal
    no_entry_cash: Decimal
    loser_exit_qty: Decimal
    loser_exit_cash: Decimal
    survivor_remaining_qty: Decimal
    estimated_fees: Decimal
    slippage_buffer: Decimal
    desired_net_profit_total: Decimal   # from pair budgets × effective qty
    seconds_to_close: float | None
    loser_exit_ts: float

@dataclass(frozen=True)
class SurvivorLegState:
    survivor_bid_0: Decimal | None      # filled M3; optional placeholder
    selected_target: Decimal
    selected_mode: SurvivorTargetMode
    loser_exit_ts: float
    seconds_to_close_0: float | None
    available_survival_time: float | None

@dataclass(frozen=True)
class SurvivorTargetPlan:
    mode: SurvivorTargetMode
    classification: SurvivorTargetClassification
    target_total_net: Decimal
    required_survivor_exit_price: Decimal
    trigger_target: Decimal              # apply slippage buffer for trigger
    total_entry_cost: Decimal
    loser_exit_proceeds: Decimal
    evidence: dict[str, str]
```

### 7.4 `SurvivorTargetPolicy` (`target_policy.py`)

```python
class SurvivorTargetPolicy:
    def __init__(self, cfg: SurvivalTargetPolicyConfig): ...

    def compute_required_exit_price(
        self,
        *,
        target_total_net: Decimal,
        total_entry_cost: Decimal,
        loser_exit_proceeds: Decimal,
        survivor_qty: Decimal,
        estimated_fees: Decimal,
        slippage_buffer: Decimal,
    ) -> Decimal: ...

    def classify_price(self, required_price: Decimal) -> SurvivorTargetClassification: ...

    def plan_for_mode(self, ctx: SurvivorLegContext, mode: SurvivorTargetMode) -> SurvivorTargetPlan: ...

    def select_plan(self, ctx: SurvivorLegContext) -> SurvivorTargetPlan:
        """For dynamic: start full_recovery; downgrade on IMPOSSIBLE/UNREALISTIC immediately.
        M3 reachability will extend downgrade logic later."""
```

**Ledger formula (locked):**

```text
required_survivor_exit_price =
  (
    target_total_net
    + total_entry_cost
    - loser_exit_proceeds
    + estimated_fees
    + slippage_buffer
  ) / survivor_qty
```

**Mode → `target_total_net`:**

| Mode | target_total_net |
|------|------------------|
| `full_recovery` | `desired_net_profit_total` |
| `breakeven` | `0` |
| `small_profit` | `+small_profit_min_usd` (config) |
| `small_loss` | `-small_loss_max_usd` (config) |

**Classification (locked):**

```text
if required_price > 1.0: IMPOSSIBLE → downgrade chain (small_loss minimum)
if required_price > max_reasonable_exit_price: UNREALISTIC → downgrade
else: VALID_CANDIDATE
```

**Principle:** Full recovery is **not** default after loser exit when classification fails.

### 7.5 Integration points

Replace call chain in `reprice_survivor_after_loser_exit`:

```python
if app.survival.enabled:  # or cfg from AppConfig
    plan = SurvivorTargetPolicy(...).select_plan(ctx)
    # apply to state.yes_target / state.no_target
else:
    # existing pnl.reprice_survivor_target_after_loser_exit
```

Build `SurvivorLegContext` from `PairedBinaryRuntimeState` matched cash fields (`yes.entry_cash`, `no.exit_cash`, etc.). If cash missing, fall back to price×qty with fact `survivor_target_ledger_fallback` (advisory) — prefer blocking downgrade to breakeven if ledger incomplete.

### 7.6 Config keys

```yaml
survival:
  enabled: false   # default globally

  target_policy:
    mode: dynamic
    small_loss_max_usd_per_pair: "0.05"
    small_profit_min_usd_per_pair: "0.03"
    max_reasonable_exit_price: "0.98"
    slippage_buffer: null   # null → inherit strategy slippage_buffer
    estimated_fee_bps: "0"
```

---

## 8. Config changes

- Add `SurvivalConfig` to `AppConfig` or nested under strategy/runtime per repo convention (inspect `config.py` — prefer top-level `survival:` key merged in loader).
- Default `enabled: false`.

---

## 9. State / data model changes

- Add optional `state.survivor_leg: SurvivorLegState | None` serialized in persistence JSON (version bump if needed; default null).
- Preserve existing `yes_target`, `no_target`, `yes_planned_target`, `no_planned_target` for monitor compatibility.

---

## 10. Facts / observability

| Fact | When |
|------|------|
| `survivor_target_selected` | After plan applied |
| `survivor_target_downgraded` | Mode chain downgrade |
| `survivor_target_impossible` | required_price > 1.0 |
| `survivor_target_unreachable` | UNREALISTIC |

**Payload must include:** mode, classification, `required_survivor_exit_price`, `trigger_target`, `total_entry_cost`, `loser_exit_proceeds`, `survivor_qty`, ledger field sources.

---

## 11. Tests

### `tests/test_survival_target_policy.py`

- Ledger math golden cases from live1 repriced target
- `required_price > 1.0` → IMPOSSIBLE → small_loss mode
- `required_price > 0.98` → UNREALISTIC
- Dynamic mode does not leave full_recovery when IMPOSSIBLE
- Slippage buffer applied to trigger only

### Integration (light)

- `survival.enabled: false` → `reprice_survivor_after_loser_exit` output unchanged vs golden

---

## 12. Acceptance criteria

- [ ] Target computation uses matched cash/qty when available.
- [ ] Impossible and unrealistic targets classified and downgraded.
- [ ] Full recovery not automatic when IMPOSSIBLE/UNREALISTIC.
- [ ] `survivor_target_selected` emitted when survival enabled.
- [ ] With `survival.enabled: false`, old repricing behavior unchanged.

---

## 13. Risks and rollback plan

| Risk | Mitigation |
|------|------------|
| Missing exit cash at reprice time | Wait for fill evidence; defer reprice until cash known |
| Persistence schema change | Optional field; default null |

**Rollback:** `survival.enabled: false`.

---

## 14. Dependencies and next milestone

**Depends on:** M0 for `seconds_to_close` in context (can pass None in unit tests).

**Next:** [Milestone 2](milestone_2_executable_exit_adapter.md) before enforcing reachability/stall.

**Parallel:** Config skeleton can merge before M0 if gated by `enabled: false`.
