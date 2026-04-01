# Milestone v1.02 — Supervised minimal order lifecycle validation

| Field | Value |
|-------|-------|
| **Milestone ID** | v1.02 |
| **Title** | Supervised minimal order lifecycle validation |
| **Status** | Deliverables merged — **§9 operator/supervisor sign-off pending** |
| **Owner** | *Assign at kickoff* |
| **Related plan** | [implementation_plan.md §7](./implementation_plan.md#7-milestone-overview) · [Specification.md](./Specification.md) |
| **Upstream dependencies** | **v1.00** §9 **Approved** · **v1.01** §9 **Approved** · allowlist instrument ID(s) **written** in runbook |
| **Blocking approvals** | §9 — blocks **automated** `CopyStrategy` orders (**v1.08**) and **supervised tiny live** (**v1.09** gate also applies) |
| **Approval required from** | Technical lead · Supervisor with trading-loss authority |
| **Target branch / PR** | `milestone/v1_02-order-lifecycle` |
| **Date created** | 2026-03-27 |
| **Last updated** | 2026-03-27 — smoke script + runbook/checklist added |

**Index:** [Milestones_INDEX.md](./Milestones_INDEX.md)

---

## 1. Objective

On **one** instrument from **v1.01**, execute a **supervised** sequence: **place** a **limit** order sized at **minimum economically meaningful size** agreed in advance, observe **acceptance** in venue/Nautilus, then **cancel** the order **or** let it fill if cancellation is impossible—proving the team controls the full **order lifecycle** before copy automation exists.

---

## 2. Scope

- Use **Nautilus `TradingNode`** + `PolymarketExecutionClient` OR an equivalent **minimal** script using the same config classes as the eventual platform (preferred: Nautilus path).
- Order **LIMIT** only (clearest semantics); side **BUY** or **SELL** per team choice; quantity at **exchange minimum** above dust.
- Pre-trade checklist: USDC.e balance, **allowances** (Nautilus `set_allowances.py` or documented manual) completed.
- Post-trade: **Venue order id** recorded; final state **CANCELED** or **FILLED** with screen/log proof.

---

## 3. Out of Scope

- **Market** orders, **FOK/FAK**, or `quote_quantity` market BUY (see **ADR-001** after **v1.07**; implementation **v1.08**)
- **CopyStrategy**, guru events, risk policies beyond a **manual** abort if something looks wrong
- **Batch** cancel, cancel-all
- Automated reconciliation service (**v1.09**)

---

## 4. Dependencies

| Dependency | Detail |
|------------|--------|
| **v1.00** | **Approved**; L2 reads work for operator |
| **v1.01** | **Approved**; `INSTRUMENT_ID` / token id **fixed** in checklist for this run |
| **Commercial** | Written **max loss** / cap for the test order signed by supervisor (**attach to evidence**) |

---

## 5. Deliverables

| Artifact | Description |
|----------|-------------|
| **Run script** | `examples/order_lifecycle_smoke.py` (`--token-id` / `TYREX_SMOKE_*`, `--execute` + confirm env) |
| **Checklist doc** | `Docs/Runbooks/order_lifecycle_v1_02.md` (operator checklist table + abort + incident template) |
| **Incident log template** | Fields: client_order_id, venue_order_id, states seen |

---

## 6. Acceptance Criteria

1. Order **submitted** and **acknowledged** (Nautilus event or API response) with matching **instrument** from v1.01.
2. **Terminal state** reached: **CANCELED** with confirmed cancel ack **or** **FILLED** with fill details captured.
3. No second concurrent test order on the same instrument without explicit supervisor sign-off (prevents duplicate confusion).
4. Script uses **environment-driven** secrets only; no keys in repo.

---

## 7. Review evidence (standard pack)

### Required test commands

- **Manual / supervised:** `python examples/order_lifecycle_smoke.py` (exact path in PR) — **no CI requirement** unless dry-run mock added later

### Required log or output artifacts

- Time-ordered logs: submit → open → cancel/fill (**redacted**)
- **Max notional** authorized vs **actual** outcome (table)

### Required config or examples

- `INSTRUMENT_ID` and order params documented in runbook **matching** v1.01 table row

### Required demo scenario

- **Live supervisor** observes run in real time or reviews timestamped logs within **24h**

### Required design or ADR references

- **N/A**; link Nautilus Polymarket **order types** / LIMIT semantics in runbook references

### Required reviewer sign-off inputs

- Trading supervisor: “Authorized max loss ___ USDC”; Technical lead: “Lifecycle terminal state ___ verified”

---

## 8. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Order fills unintentionally | Economic loss | Limit far from market or size minimal; supervisor monitoring |
| Signing latency causes double submit | Duplicate exposure | Single-flight submit in script; manual run only |
| Insufficient allowance | BLOCKED | Run allowance script first; verify balance |

---

## 9. Approval gate

| Role | Responsibility |
|------|----------------|
| **Technical lead** | Confirms technical correctness of logs and instrument |
| **Supervisor** | Accepts economic risk of test |

**What must be reviewed**

- Terminal state proof (UI or API JSON **redacted**)
- Checklist completed

**Conditions that block v1.07 / v1.08**

- **ADR-001** drafting may start after **v1.06**, but **v1.08** merge requires **v1.07** approval — **v1.02** evidence must exist to prove team has operated real orders before ADR is treated as informed

**Conditions that block supervised tiny live (v1.09)**

- **v1.02 Approved** is **necessary but not sufficient**; **v1.09** has additional operational gates

**Sign-off template**

> Milestone **v1.02** **Approved**. Supervised order lifecycle on instrument ___ ; terminal state ___ ; date ___ . Supervisor: ___ · Lead: ___
