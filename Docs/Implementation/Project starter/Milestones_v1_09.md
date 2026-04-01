# Milestone v1.09 — Live-safe orchestration

| Field | Value |
|-------|-------|
| **Milestone ID** | v1.09 |
| **Title** | Live-safe orchestration |
| **Status** | Not Started |
| **Owner** | *Assign at kickoff* |
| **Related plan** | [implementation_plan.md §3](./implementation_plan.md#3-implementation-approach) · Spec §6 Live |
| **Upstream dependencies** | **v1.08** §9 **Approved** · **ADR-001** still current (no open superseding revision) |
| **Blocking approvals** | §9 — **only gate** that authorizes **first supervised tiny-live automated copy** (with caps) |
| **Approval required from** | Technical lead · Operations supervisor · Trading supervisor |
| **Target branch / PR** | `milestone/v1_09-live-safe` |
| **Date created** | 2026-03-27 |
| **Last updated** | 2026-03-27 |

**Index:** [Milestones_INDEX.md](./Milestones_INDEX.md)

---

## 1. Objective

Live path: **startup sequence**, **reconciliation snapshot**, **StateStore**, **kill switch**, **NotifierActor**, graceful shutdown—**defensible supervised tiny live** copy.

---

## 2. Scope

- `runtime/live.py`, `execution/reconciliation.py`, `core/state_store.py`, `runtime/notifier.py`
- `Docs/runbooks/live_startup.md` (numbered 1–7)
- Integration tests: kill switch, restart dedup, notifier mock

---

## 3. Out of Scope

- Auto-healing reconciliation
- HA / vault
- Dashboards

---

## 4. Dependencies

| Dependency | Detail |
|------------|--------|
| **v1.08** | **Approved** — execution code merged |
| **v1.05–v1.06** | Telemetry + risk stable |
| **Evidence bundle** | Template: `Docs/evidence/v1_09_live_safe_checklist.md` filled before tiny live |

---

## 5. Deliverables

| Artifact | Description |
|----------|-------------|
| Code + runbook | As scope |
| `tests/integration/test_state_store_roundtrip.py` | |

---

## 6. Acceptance Criteria

1. `KILL_SWITCH=1` → **zero** orders; log `kill_switch_active` (test).
2. Restart + duplicate guru id → **zero** extra `submit_order` (mock counter).
3. Reconciliation counts logged **before** strategy enabled.
4. Notifier ping in CI mock.
5. Runbook ≤2 pages.

---

## 7. Review evidence (standard pack)

### Required test commands

```bash
pytest tests/integration/test_state_store_roundtrip.py -v
# plus any new live-runtime integration tests
```

### Required log or output artifacts

- **Redacted** dry-run: startup steps **1–7** visible in order

### Required config or examples

- `Docs/evidence/v1_09_live_safe_checklist.md` completed

### Required demo scenario

- **Supervised** dry run with ops + trading supervisor present (or async review of recording within **48h**)

### Required design or ADR references

- **ADR-001** § reconciliation + **this** runbook cross-linked

### Required reviewer sign-off inputs

- Three-role sign-off on checklist file

---

## 8. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| False alarms | Fatigue | Warn-only for non-critical first |

---

## 9. Approval gate

**Authorizes (only when Approved)**

- **First** production-like **automated copy** orders **subject to written caps** on checklist

**Does not claim**

- General production readiness, scale, or compliance

**Conditions that block tiny live**

- **v1.09** not **Approved**
- Kill switch **untested**
- Caps unsigned

**Sign-off template**

> **v1.09 Approved** for **supervised tiny live** copy: caps ___ USDC / markets ___ / date ___ . Signatories: ___ · ___ · ___
