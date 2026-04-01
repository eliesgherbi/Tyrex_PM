# Milestone v1.05 — Signal contracts and copy decision flow (shadow mode)

| Field | Value |
|-------|-------|
| **Milestone ID** | v1.05 |
| **Title** | Signal contracts and copy decision flow (shadow mode) |
| **Status** | Not Started |
| **Owner** | *Assign at kickoff* |
| **Related plan** | [implementation_plan.md §6–§7](./implementation_plan.md#6-key-interfaces--contracts-summary) |
| **Upstream dependencies** | **v1.04** §9 **Approved** · **v1.01** deliverable **allowlist file** exists on branch (committed path) |
| **Blocking approvals** | §9 — before **v1.06** risk composition; before **`execution.mode=live`** in templates |
| **Approval required from** | Technical lead · **Second engineer** (four-eyes) |
| **Target branch / PR** | `milestone/v1_05-shadow-copy` |
| **Date created** | 2026-03-27 |
| **Last updated** | 2026-03-27 |

**Index:** [Milestones_INDEX.md](./Milestones_INDEX.md)

---

## 1. Objective

Implement **entry/exit signal policies** and a **`CopyStrategy`** that performs the full **decision pipeline** (signal → sizing → stub execution port) **except** that **no Polymarket orders are submitted** from the copy path. Every branch produces **structured telemetry** with **`reason_code`** and **`correlation_id = guru_trade_id`**.

---

## 2. Scope

- **`EntrySignalPolicy`** / **`GuruFollowEntryPolicy`**, **`ExitSignalPolicy`** / **`GuruMirrorExitPolicy`**, **`SizingPolicy`**, **`NoOpExecutionPort`**
- **`CopyStrategy`** subscribes to `GuruTradeSignal`; calls **`ExecutionPort.submit_intent`** in shadow mode
- **`reason_code` taxonomy** in `core/reason_codes.py` (or `Docs/` + enum)

---

## 3. Out of Scope

- **`RiskPolicy` deny/resize** (**v1.06**) — strategy may use sizing cap only; **must not** claim fail-closed risk
- **`PolymarketExecutionPolicy`** real orders
- **Notifier** (**v1.09**)
- **Persistence** (**v1.09**)

---

## 4. Dependencies

| Dependency | Detail |
|------------|--------|
| **v1.04** | **Approved**; **`GuruTradeSignal`** schema frozen in merge commit (breaking change → new milestone review) |
| **v1.01** | `config/v1_markets.yaml` (or agreed) **merged** so allowlist filtering is testable |

---

## 5. Deliverables

| Artifact | Description |
|----------|-------------|
| `signal/*.py` | Policies |
| `strategy/copy_strategy.py` | Shadow path |
| `core/reason_codes.py` | Enum |
| `tests/unit/test_entry_policy.py` | Table tests |
| `tests/unit/test_copy_strategy_shadow.py` | No order submission |

---

## 6. Acceptance Criteria

1. Per `GuruTradeSignal` in tests: **one** telemetry record with `copy_skip` | `shadow_order_intent` and non-empty `reason_code`.
2. Default config: `execution.mode=shadow` only; **`NoOpExecutionPort`** wired.
3. **`rg 'order_factory|submit_order|MarketOrder'`** on `strategy/copy_strategy.py` — **no matches** (attach output).
4. Not-allowlisted token → `NOT_ALLOWLISTED`.

---

## 7. Review evidence (standard pack)

### Required test commands

```bash
pytest tests/unit/test_entry_policy.py tests/unit/test_copy_strategy_shadow.py -v
rg "order_factory|submit_order|MarketOrder" src/platform/strategy/copy_strategy.py || true
# Expect: no matches (empty rg output). If matches exist → milestone failed.
```

### Required log or output artifacts

- Markdown table: **5** synthetic guru rows → outcome + `reason_code`

### Required config or examples

- `execution.mode=shadow` in example config

### Required demo scenario

- Second engineer walks one test case in debugger or log trace

### Required design or ADR references

- **N/A** (ADR-001 is **v1.07**)

### Required reviewer sign-off inputs

- Two reviewer names on PR + explicit “no submit path” checkbox

---

## 8. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Accidental order wiring | Real trades | `rg` gate + dual review |

---

## 9. Approval gate

**Conditions that block v1.06**

- **v1.05** not **Approved**
- `reason_codes` enum changed without updating tests + review

**Conditions that block `execution.mode=live`**

- **v1.05 + v1.06 + v1.07 + v1.08 + v1.09** per their gates (this milestone alone **does not** authorize live)

**Sign-off template**

> Milestone **v1.05** **Approved**. Shadow copy path. Reviewers: ___ / ___ (date ___)
