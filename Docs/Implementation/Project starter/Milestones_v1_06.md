# Milestone v1.06 — Risk intent pipeline

| Field | Value |
|-------|-------|
| **Milestone ID** | v1.06 |
| **Title** | Risk intent pipeline |
| **Status** | Not Started |
| **Owner** | *Assign at kickoff* |
| **Related plan** | [implementation_plan.md §4](./implementation_plan.md#4-architecture-summary) · Spec §4.4 |
| **Upstream dependencies** | **v1.05** §9 **Approved** · **`reason_codes`** and shadow telemetry merged |
| **Blocking approvals** | §9 — before **ADR-001** treats risk outputs as inputs (**v1.07**) |
| **Approval required from** | Technical lead |
| **Target branch / PR** | `milestone/v1_06-risk` |
| **Date created** | 2026-03-27 |
| **Last updated** | 2026-03-27 |

**Index:** [Milestones_INDEX.md](./Milestones_INDEX.md)

---

## 1. Objective

Implement **fail-closed** **risk** between sizing and execution: missing input, stale quote snapshot, limits, or **kill_switch** → **`RiskDecision.DENY`** with **`reason_code`**. **`CLAMP`** only if documented and logged pre/post size.

---

## 2. Scope

- **`RiskPolicy`**, **`RiskSnapshot`**, **`PortfolioGuard`**, **`CopyRiskPolicy`**
- Integration in **`CopyStrategy`** before execution port — logs `risk_deny` | `risk_clamp` | `risk_pass`
- **≥8** unit cases (parametrize allowed) with frozen numbers

---

## 3. Out of Scope

- Venue order type selection (**v1.07**–**v1.08**)
- Automated protective PnL flatten (manual kill_switch only)
- **Multi-strategy** allocation

---

## 4. Dependencies

| Dependency | Detail |
|------------|--------|
| **v1.05** | **Approved** — `CopyStrategy` and `OrderIntent` shape stable |
| **v1.01** | `instrument_id` keys for positions in tests |
| **Evidence** | `pytest` shadow tests **green** on parent branch before merge |

---

## 5. Deliverables

| Artifact | Description |
|----------|-------------|
| `risk/*.py` | Policies + guards |
| `core/intent.py` | `OrderIntent`, `RiskDecision` |
| `tests/unit/test_risk_*.py` | ≥8 cases |
| Docstring | **Deterministic rule order** |

---

## 6. Acceptance Criteria

1. `kill_switch=true` → 100% `DENY`, `KILL_SWITCH`
2. Stale quote → `STALE_QUOTE` (injected clock test)
3. Position cap: **no** silent exceed — documented `DENY` or `CLAMP`
4. **No** `httpx`/`py_clob` under `risk/` (guard test optional)
5. `DENY` → execution port **not** called (test)

---

## 7. Review evidence (standard pack)

### Required test commands

```bash
pytest tests/unit/test_risk*.py -v --tb=short
```

### Required log or output artifacts

- Table: rule order, DENY vs CLAMP per rule

### Required config or examples

- Example `stale_quote_ms`, caps in `config/v1.example.yaml`

### Required demo scenario

- Reviewer traces one `DENY` and one `CLAMP` in tests

### Required design or ADR references

- **v1.07 ADR-001** must reference **`RiskDecision`** outputs — **after** this milestone merges, ADR draft updates

### Required reviewer sign-off inputs

- Lead confirms rule order matches Spec **fail-closed** intent

---

## 8. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Stale thresholds wrong | Skips or bad fills | Conservative defaults |

---

## 9. Approval gate

**Conditions that block v1.07 drafting (risk section)**

- **v1.06** not **Approved**

**Sign-off template**

> Milestone **v1.06** **Approved**. Risk pipeline; tests ___ passed. Reviewer: ___ · Date ___
