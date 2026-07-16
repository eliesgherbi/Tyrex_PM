# Milestone 5 — Fee/slippage-aware economics gate

**Program:** [phase1.md](phase1.md) §6 Phase 1.5  
**Status:** specification — not implemented  
**Depends on:** [M1](milestone_1_survival_models_and_target_policy.md), [M2](milestone_2_executable_exit_adapter.md)

---

## 1. Purpose

Implement **ExitEconomics** — ledger-based net PnL estimation for pre-entry, post-loser survivor ticks, and pre-close — using matched cashflows, executable survivor exit estimates, fees, and slippage.

---

## 2. Why this milestone exists

Survival modules need a unified **economic realism** check: even reachable targets may be negative EV after spread/slippage/fees. Separates "price can get there" from "trade is worth holding."

---

## 3. Scope

- `survival/economics.py`
- Hook points: entry eval (optional), survivor monitor tick, pre-close
- Facts; advisory default

---

## 4. Non-goals

- Positive-EV optimization / alpha
- Hard deny at risk engine unless explicitly configured later
- Fee schedule integration beyond config bps (optional enhancement)

---

## 5. Current code areas to review

| File | Notes |
|------|-------|
| `strategies/paired_binary/pnl.py` | `realized_pnl_from_cashflows`, budgets |
| `strategies/paired_binary/entry_eval.py` | Pre-entry gate |
| `survival/target_policy.py` | Ledger context |
| `survival/exit_planning.py` | Executable exit |

---

## 6. Target architecture

```text
ExitEconomics.evaluate(ctx: EconomicsContext) → EconomicsEvaluation
  verdict: proceed | defer | abort_entry | exit_survivor_early
  expected_net_total, expected_net_per_pair, evidence
```

---

## 7. Detailed implementation plan

### 7.1 Files to create

| File | Purpose |
|------|---------|
| `src/tyrex_pm/survival/economics.py` | Gate logic |
| `tests/test_survival_economics.py` | |

### 7.2 Files to modify

| File | Changes |
|------|---------|
| `survival/models.py` | `EconomicsContext`, `EconomicsEvaluation`, `EconomicsVerdict` |
| `strategies/paired_binary/entry_eval.py` | Optional call when survival+economics enabled |
| `strategies/paired_binary/monitor.py` | Survivor tick hook |
| `runtime/config.py` | `survival.economics` |

### 7.3 Types

```python
class EconomicsVerdict(str, Enum):
    PROCEED = "proceed"
    DEFER = "defer"
    ABORT_ENTRY = "abort_entry"
    EXIT_SURVIVOR_EARLY = "exit_survivor_early"

@dataclass(frozen=True)
class EconomicsContext:
    phase: Literal["pre_entry", "survivor_hold", "pre_close"]
    total_entry_cost: Decimal | None
    loser_exit_proceeds: Decimal | None
    survivor_qty: Decimal | None
    exit_eval: SurvivalExitEvaluation | None
    selected_target_mode: SurvivorTargetMode | None
    estimated_fees: Decimal
    slippage_buffer: Decimal
    minimum_acceptable_net: Decimal

@dataclass(frozen=True)
class EconomicsEvaluation:
    verdict: EconomicsVerdict
    expected_net_total: Decimal | None
    expected_net_per_pair: Decimal | None
    evidence: dict[str, str]

class ExitEconomics:
    def evaluate(self, ctx: EconomicsContext, cfg: EconomicsConfig) -> EconomicsEvaluation: ...
```

### 7.4 Net estimate (ledger + executable)

```text
expected_survivor_proceeds = executable_bid_or_vwap × survivor_qty - fees - slippage
expected_net_total =
  loser_exit_proceeds + expected_survivor_proceeds - total_entry_cost
```

Pre-entry: estimate both legs entry cost + hypothetical exit at executable books.

### 7.5 Integration points

| Phase | Behavior |
|-------|----------|
| `pre_entry` | If `reject_entry_if_expected_net_below` set and enforce → skip entry |
| `survivor_hold` | If net < `exit_survivor_if_expected_net_below` and enforce → early exit |
| `pre_close` | Force exit recommendation if net cannot improve |

Default: `enforcement_mode: advisory` — facts only.

### 7.6 Config

```yaml
survival:
  economics:
    enabled: true
    enforcement_mode: advisory
    min_acceptable_net_usd_per_pair: "-0.02"
    estimated_fee_bps: "0"
    reject_entry_if_expected_net_below: null
    exit_survivor_if_expected_net_below: "-0.05"
```

---

## 8. Config changes

Add `EconomicsConfig` under `SurvivalConfig`.

---

## 9. State / data model changes

None required beyond context built from runtime state.

---

## 10. Facts / observability

| Fact | When |
|------|------|
| `survivor_economics_evaluated` | Each evaluation (deduped) |
| `survivor_early_exit_triggered` | Enforce early exit |

Payload: expected_net_total, expected_net_per_pair, verdict, fees, slippage, executable bid, target mode, enforcement_mode.

---

## 11. Tests

### `tests/test_survival_economics.py`

- Ledger + executable components
- Advisory: verdict logged, no exit
- Enforce: exit when below threshold
- Pre-entry abort when configured

---

## 12. Acceptance criteria

- [ ] Uses ledger cashflows + M2 executable estimate.
- [ ] Advisory emits facts only.
- [ ] Enforce triggers early exit in tests.
- [ ] Pre-entry hook skipped when `survival.enabled: false`.

---

## 13. Risks and rollback plan

| Risk | Mitigation |
|------|------------|
| Over-blocking entries | null reject threshold by default |

**Rollback:** `economics.enabled: false`.

---

## 14. Dependencies and next milestone

**Depends on:** M1, M2.

**Next:** [Milestone 6](milestone_6_kill_switches.md) or M7.
