# Milestone v1.08 — Execution pipeline implementation

| Field | Value |
|-------|-------|
| **Milestone ID** | v1.08 |
| **Title** | Execution pipeline implementation |
| **Status** | Not Started |
| **Owner** | *Assign at kickoff* |
| **Related plan** | [implementation_plan.md §3](./implementation_plan.md#3-implementation-approach) |
| **Upstream dependencies** | **v1.07** §9 **Approved** · **ADR-001** merged at **`Docs/ADR/ADR-001-execution-semantics-polymarket.md`** · review minutes merged · **v1.06** **Approved** |
| **Blocking approvals** | §9 — blocks **`execution.mode=live`** deployment (**v1.09** adds operational gate) |
| **Approval required from** | Technical lead · Engineer **not** primary author of PR |
| **Target branch / PR** | `milestone/v1_08-execution` |
| **Date created** | 2026-03-27 |
| **Last updated** | 2026-03-27 |

**Index:** [Milestones_INDEX.md](./Milestones_INDEX.md)

---

## 1. Objective

Implement **`PolymarketExecutionPolicy`** per **ADR-001** only: **`OrderIntent` → Nautilus `Order`**, `submit_order` via strategy; **no** change to signal/risk semantics.

---

## 2. Scope

- `execution/polymarket_execution.py`, `CopyStrategy` live branch
- Tests: mock `order_factory`; table vs ADR mapping rows
- Idempotency per ADR
- `config/v1.example.yaml` keys documented

---

## 3. Out of Scope

- Notifier, persistence, reconciliation orchestration (**v1.09**)
- Backtest harness (**v1.10**)
- `py_clob` in app unless **ADR-001** lists exception

---

## 4. Dependencies

| Dependency | Detail |
|------------|--------|
| **ADR-001** | **Merged**; PR must cite **ADR ID** in title/body |
| **v1.07** | **Approved** sign-off on file |
| **v1.06** | Risk gating tests still green |

---

## 5. Deliverables

| Artifact | Description |
|----------|-------------|
| `execution/polymarket_execution.py` | |
| `tests/unit/test_execution_policy_orders.py` | |
| `tests/unit/test_copy_idempotency.py` | |

---

## 6. Acceptance Criteria

1. **Each** ADR mapping row has ≥1 test mapping intent → order fields.
2. Market BUY path: `quote_quantity=True` when ADR requires.
3. `RiskDecision.DENY` → no submit (regression).
4. `execution.mode=shadow` unchanged vs v1.05.
5. `rg py_clob_client src` empty **or** ADR **Exception** clause + reviewer initials in minutes.

---

## 7. Review evidence (standard pack)

### Required test commands

```bash
pytest tests/unit/test_execution_policy_orders.py tests/unit/test_copy_idempotency.py -v
pytest tests/unit/test_copy_strategy_shadow.py tests/unit/test_risk*.py  # regression
```

### Required log or output artifacts

- PR checklist: ADR table row ↔ test case **tick list**

### Required config or examples

- Live vs shadow flags documented; **`execution.mode=live` default remains false** until v1.09

### Required demo scenario

- Non-author reviewer runs **one** parametrized test in IDE

### Required design or ADR references

- **ADR-001** — PR must link by **path + commit SHA**

### Required reviewer sign-off inputs

- Second reviewer approval on PR

---

## 8. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| ADR drift | Wrong economics | Block merge without ADR update |

---

## 9. Approval gate

**Does not authorize live trading**

- **v1.09** required for supervised tiny live + persistence

**Conditions that block merging v1.08**

- **v1.07** not **Approved** or ADR missing from branch

**Sign-off template**

> Milestone **v1.08** **Approved**. Execution matches **ADR-001** @ ___ (SHA). Reviewers: ___ · ___
