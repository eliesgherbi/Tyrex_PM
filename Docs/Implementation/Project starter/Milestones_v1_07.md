# Milestone v1.07 — Execution semantics and reconciliation specification

| Field | Value |
|-------|-------|
| **Milestone ID** | v1.07 |
| **Title** | Execution semantics and reconciliation specification |
| **Status** | Not Started |
| **Owner** | *Assign at kickoff* |
| **Related plan** | [implementation_plan.md §3](./implementation_plan.md#3-implementation-approach) · Gate **before v1.08** |
| **Upstream dependencies** | **v1.02** §9 **Approved** (supervised order evidence) · **v1.06** §9 **Approved** · **RiskDecision** types merged |
| **Blocking approvals** | §9 — **hard block** on **v1.08** code & **CopyStrategy** `submit_order` |
| **Approval required from** | Technical lead · Second senior engineer · Trading supervisor |
| **Target branch / PR** | `docs/adr-001` or `milestone/v1_07-adr` |
| **Date created** | 2026-03-27 |
| **Last updated** | 2026-03-27 |

**Index:** [Milestones_INDEX.md](./Milestones_INDEX.md)

---

## 1. Objective

Produce **ADR-001** (`Docs/ADR/ADR-001-execution-semantics-polymarket.md`) **merged to default branch** that **freezes** `OrderIntent` → Nautilus `Order` mapping, band/TIF, partial fills, idempotency, and **reconciliation expectations**—**no** production execution implementation in this milestone (stubs with `NotImplementedError` allowed only if listed in ADR appendix).

---

## 2. Scope

- ADR mandatory sections **1–9** as in prior milestone text (order table, band+ε example, fallback limit, market buy `quote_quantity`, market sell base qty, precision, partial fills, reconciliation, idempotency)
- **`ExecutionPolicy`** Python signature in ADR appendix
- **Review minutes** merged: `Docs/ADR/reviews/ADR-001_review_minutes.md` (or PR thread export **linked** from ADR footer)

---

## 3. Out of Scope

- `PolymarketExecutionPolicy` implementation (**v1.08**)
- Backtest fill model (**v1.10**)
- SLA / SLO

---

## 4. Dependencies

| Dependency | Detail |
|------------|--------|
| **v1.02** | **Approved** — team has **evidence** of real LIMIT lifecycle (attach link in ADR references) |
| **v1.06** | **Approved** — `RiskDecision` / `OrderIntent` documented in ADR inputs |
| **External** | Nautilus Polymarket doc + Polymarket CLOB order docs **cited by URL** in ADR |

---

## 5. Deliverables

| Artifact | Description |
|----------|-------------|
| `Docs/ADR/ADR-001-execution-semantics-polymarket.md` | Binding spec |
| `Docs/ADR/reviews/ADR-001_review_minutes.md` | Decisions + attendees |

---

## 6. Acceptance Criteria

1. ADR contains all **nine** mandatory sections (content present; headings may differ).
2. Numeric default for **ε** + config key name stated.
3. Polymarket **inactive order** limitation + v1 **forbid** `generate_order_history_from_trades` explicitly.
4. **Three** signatories recorded in review minutes with **date**.
5. **No** merging **v1.08** until **this** §9 is **Approved** (enforced by process).

---

## 7. Review evidence (standard pack)

### Required test commands

- **N/A** (doc milestone). Optional: `markdownlint` if repo configures it.

### Required log or output artifacts

- **N/A**

### Required config or examples

- ADR lists **config keys** that v1.08 will implement (names only)

### Required demo scenario

- **Walkthrough:** author presents ADR sections 1–9 in **30 min** review meeting

### Required design or ADR references

- **ADR-001** is the deliverable; must reference **Nautilus + Polymarket official URLs**

### Required reviewer sign-off inputs

- Review minutes with three roles + **explicit** “v1.08 authorized to start” or “revisions required”

---

## 8. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Ambiguous ε | Wrong pricing | Worked numeric example mandatory |
| Skipping meeting | Silent drift | No v1.08 until minutes filed |

---

## 9. Approval gate

**Hard block**

- **Any** `PolymarketExecutionPolicy` implementation PR **v1.08**
- **Any** `submit_order` from `CopyStrategy` for copy automation

**What must be reviewed**

- Full ADR + minutes + cross-check vs **v1.06** risk outputs

**Conditions that block v1.08**

- Fewer than **three** approved signatories
- ADR not on **default** branch

**Sign-off template (file in review minutes)**

> **ADR-001** approved as binding for v1 execution on ___ . Signatories: ___ · ___ · ___ . **v1.08 implementation** may begin.
