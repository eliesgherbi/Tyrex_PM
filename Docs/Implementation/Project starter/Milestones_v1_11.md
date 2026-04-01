# Milestone v1.11 — Reporting and V1 closure

| Field | Value |
|-------|-------|
| **Milestone ID** | v1.11 |
| **Title** | Reporting and V1 closure |
| **Status** | Not Started |
| **Owner** | *Assign at kickoff* |
| **Related plan** | [implementation_plan.md](./implementation_plan.md) · Spec §8 |
| **Upstream dependencies** | **v1.10** §9 **Approved** · Reporting inputs available |
| **Blocking approvals** | §9 — **product v1 narrative** only after review |
| **Approval required from** | Technical lead · Product / spec owner |
| **Target branch / PR** | `milestone/v1_11-closure` |
| **Date created** | 2026-03-27 |
| **Last updated** | 2026-03-27 |

**Index:** [Milestones_INDEX.md](./Milestones_INDEX.md)

---

## 1. Objective

Deliver **reporting** (PnL, fills, slippage definition, skip histogram), **dependency pins**, **`V1_CLOSURE.md`** mapping Specification §8 to **evidence**—without **overstating** operational maturity.

---

## 2. Scope

- `reporting/run_report.py`, `Docs/V1_CLOSURE.md`, `Docs/dependency_lock.md`
- Optional log parser

---

## 3. Out of Scope

- Dashboards, compliance tear sheets, guru alpha attribution

---

## 4. Dependencies

| Dependency | Detail |
|------------|--------|
| **v1.10** | **Approved** |
| **For any “live” or “live-ready” language in `V1_CLOSURE.md`** | **v1.09** §9 **Approved** **and** `Docs/evidence/v1_09_live_safe_checklist.md` (or successor) **must** be **linked** as evidence — otherwise mark live items **N/A** |
| **ADR-001** | Referenced for execution isolation bullet |

---

## 5. Deliverables

| Artifact | Description |
|----------|-------------|
| Reporting module + CSV/JSON | |
| `Docs/V1_CLOSURE.md` | §8 checklist |
| `Docs/dependency_lock.md` | Pins |

---

## 6. Acceptance Criteria

1. Spec §8: each bullet ✅ with link **or** ❌ deferred with reason — **no** ambiguous language.
2. **`report` command** exits 0 on fixture (document exact invocation).
3. `skip_reason` OTHER ≤5% on fixture **or** explained.
4. Pins match lockfile if present.

**Live-readiness rule**

- The phrase **“live ready”**, **“production”**, or **“deployable for real money”** may appear **only** if **v1.09 Approved** evidence is **linked** in the same document section. Otherwise use **“backtest-complete only”** or **“live N/A”**.

---

## 7. Review evidence (standard pack)

### Required test commands

```bash
pytest  # or minimal subset documented
python -m reporting.run_report --help  # exact CLI TBD in implementation
```

### Required log or output artifacts

- **Sanitized** `report.csv` from fixture run

### Required config or examples

- Fixture path documented in `V1_CLOSURE.md`

### Required demo scenario

- Product owner walks §8 checklist with evidence links

### Required design or ADR references

- **ADR-001** linked for architecture bullets

### Required reviewer sign-off inputs

- Product owner: confirms **no overclaim** on live vs backtest

---

## 8. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Overclaim | Bad go-live | Live language gated on **v1.09** |

---

## 9. Approval gate

**Permitted closure states (pick one, explicit in `V1_CLOSURE.md`)**

- **A — Backtest + architecture v1 complete; live N/A**
- **B — Backtest + architecture v1 complete; supervised tiny live approved under v1.09 evidence** (link required)

**Sign-off template**

> **v1.11 Approved**. Closure state: **A** / **B**. Product: ___ · Lead: ___ · Date ___

> Post-v1 backlog: ___
