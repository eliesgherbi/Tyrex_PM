# Milestone v1.10 — Backtest runtime and historical replay

| Field | Value |
|-------|-------|
| **Milestone ID** | v1.10 |
| **Title** | Backtest runtime and historical replay |
| **Status** | Not Started |
| **Owner** | *Assign at kickoff* |
| **Related plan** | [implementation_plan.md §7](./implementation_plan.md#7-milestone-overview) · Spec §6 Backtest |
| **Upstream dependencies** | **v1.08** §9 **Approved** (full pipeline: risk + execution + **ADR-001** aligned) · **v1.03** **Approved** |
| **Blocking approvals** | §9 — before **v1.11** claims **Specification §8** item on **same strategy class** in backtest |
| **Approval required from** | Technical lead |
| **Target branch / PR** | `milestone/v1_10-backtest` |
| **Date created** | 2026-03-27 |
| **Last updated** | 2026-03-27 |

**Index:** [Milestones_INDEX.md](./Milestones_INDEX.md)

---

## 1. Objective

Provide **`BacktestRuntime`** using **`PolymarketDataLoader`**, replay **recorded `GuruTradeSignal`** stream, run the **same `CopyStrategy` import path** as live, optional **`submit_delay_ms`**—for **simulation and research**, not for claiming live readiness.

---

## 2. Scope

- `runtime/backtest.py`, `HistoricalMarketLoader`, `HistoricalGuruLoader`
- `examples/backtest_copy.py`, fixtures, `tests/integration/test_backtest_smoke.py`
- Capture fills in memory for **v1.11**

---

## 3. Out of Scope

- Perfect L2 microstructure; distributed backtest; multi-guru optimization
- **Any statement of live readiness** — backtest-only

---

## 4. Dependencies

| Dependency | Detail |
|------------|--------|
| **v1.08** | **Approved** — `CopyStrategy` includes **non-shadow** execution path used in backtest |
| **v1.07** | **ADR-001** merged (order sizing in sim) |
| **v1.05** | Shadow telemetry patterns (regression) |
| **Explicit** | This milestone **does not** require **v1.09 Approved** — offline CI uses fixtures only |

---

## 5. Deliverables

| Artifact | Description |
|----------|-------------|
| Loaders + `runtime/backtest.py` | |
| `fixtures/backtest/` | JSONL guru + trade subset |
| `examples/backtest_copy.py` | |

---

## 6. Acceptance Criteria

1. **Same** `CopyStrategy` import path documented for live and backtest (grep / doc).
2. Smoke test **exit 0**; ≥1 `copy_skip` or `copy_decision` from fixture.
3. Delay test: mocked clock ≥ `submit_delay_ms` between signal dispatch and order build.
4. Fixtures bounded (≤500 guru rows, ≤5000 trades in **CI** profile).
5. Default `pytest` **offline** (no live HTTP).

---

## 7. Review evidence (standard pack)

### Required test commands

```bash
pytest tests/integration/test_backtest_smoke.py -v
```

### Required log or output artifacts

- **Redacted** engine report snippet + **one** paragraph: **known gaps vs live** (e.g. no chain delay, no WS drops)

### Required config or examples

- `submit_delay_ms` in example config

### Required demo scenario

- Reviewer runs smoke locally **< 30s**

### Required design or ADR references

- **ADR-001** — sim assumptions must not contradict table (note deviations explicitly)

### Required reviewer sign-off inputs

- Explicit checkbox: “**Backtest passing does not imply live readiness**” signed by reviewer

---

## 8. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Loader flakiness | CI fail | Fixtures only in CI |

---

## 9. Approval gate

**Explicit non-claims**

- **v1.10 Approved** **≠** production live **≠** **v1.09** substitute

**Conditions that block v1.11 “parity” wording**

- **v1.10** not Approved **or** live path not merged (**v1.08**)

**Sign-off template**

> **v1.10 Approved**. Backtest smoke green. Reviewer acknowledges backtest ≠ live: ___ (date)
