# N7 — Tiny operator-controlled live

**Status:** planned (no implementation or live enablement in this planning commit)  
**Milestone folder:** `Docs/implementation/n7_tiny_operator_live/`  
**Depends on:** N1–N6 accepted evidence + explicit operator authorization  
**Unblocks:** Later limited continuous live (separate acceptance) / Scope B (separate)

---

## 1. Objective

Define and later execute the **final controlled release** milestone: a tiny,
operator-gated, fail-closed Z-Gap live run under **Scope A** (mandatory
pre-resolution exit; no redeem dependency).

Default OFF. One-shot first. No unattended continuous live until separately
accepted.

---

## 2. Why the milestone exists

Engineering readiness (N1–N6) is not the same as authorized money at risk.
N7 is the human-controlled gate that binds limits, preflight, kill, recon, and
rollback into a repeatable first-run procedure.

---

## 3. Scope

### Required controls

| Control | Requirement |
|---------|-------------|
| Explicit operator opt-in | Typed phrase / flag; no silent default |
| Default OFF | `live.enabled=false`, `mutations_enabled=false` |
| One-shot mode first | Single window; process exits after terminal |
| Fixed tiny max order value | Provisional **$5** fee-inclusive BUY cap (decision #10) |
| One position per window | Existing Z-Gap policy |
| Max daily exposure/loss | Configured hard caps |
| No same-window reverse/re-entry | Existing policy |
| Kill switch | Operator + risk |
| Preflight balance/inventory recon | Must pass before submit |
| Feed / PTB / time readiness | Must be READY |
| Order ack timeout | Ambiguous → stop |
| Ambiguous-order stop | No automated guess |
| Post-trade recon | Mandatory |
| Operator report | Full evidence pack |
| Rollback / disable | Documented procedure |
| No unattended continuous | Until separate acceptance |

### Live scope

**Scope A only** for first N7 acceptance (see N6). Scope B is out of scope
until redeem/finality work is done.

---

## 4. Explicit non-goals

- Continuous multi-window live
- Scope B resolution-hold live
- Auto-approval / non-interactive trading that actually submits
- Raising caps above tiny without new acceptance
- Profitability significance as a go criterion
- Importing `old/` tiny-live CLI as the product

---

## 5. Dependencies and entry criteria

Evidence required from prior milestones:

| Milestone | Evidence |
|-----------|----------|
| N1 | PTB source proof + latency source choice |
| N2 | Adapter reliability (reconnect/heartbeat) |
| N3 | PTB lock + basis gates fail-closed |
| N4 | Real OBSERVE engineering sample passed |
| N5 | Real SHADOW scenarios passed; limitations documented |
| N6 | Generic live Scope A fake-transport + recon accepted; mutations composable |

Additional:

- Clean git worktree for mutate path
- Credentials roles validated on operator host
- Clock sync acceptable on deployment host
- Operator available for entire one-shot window

---

## 6. Decisions that must already be frozen

1. Scope A first (#8)  
2. Order type / marketable limit (#9)  
3. Tiny per-order / per-window / daily limits (#10)  
4. One-shot operator workflow (#11)  
5. Deployment / clock sync expectations (#12)  
6. Kill and ambiguous-order stop behavior (N6)  
7. PTB confirmed required for live entry (N3)

---

## 7. Responsibility / module ownership

| Concern | Owner |
|---------|-------|
| Opt-in / CLI | application + runtime live host |
| Caps / kill | RiskEngine |
| Orders | LiveOMS |
| Recon | ReconciliationService + CLI |
| Strategy | Z-Gap intents only |
| Report | Fact sink + operator summary |
| Approval ceremony | Ops docs + CLI gates |

---

## 8. Contracts, ports, and data structures to add or evolve

| Item | Notes |
|------|-------|
| Live run manifest | config hash, scope=A, limits, git HEAD, operator id |
| Opt-in artifact | Explicit approval record (not auto-generated silently) |
| Preflight report | Pass/fail gates |
| Post-run recon report | Inventory/orders/PnL |
| Abort codes | Stable enum for stop reasons |

---

## 9. Expected files / modules affected (when implemented)

```text
src/tyrex_pm/runtime/          # z_gap or generic one-shot live host
src/tyrex_pm/application/cli.py
src/tyrex_pm/risk/policies.py
config/                        # sealed tiny-live config (not r7/)
Docs/latest/how_to/run_modes.md
Docs/implementation/n7_tiny_operator_live/  # runbook + evidence
tests/                         # gates, opt-in, caps (no live money in CI)
```

---

## 10. End-to-end data or control flow

```text
Operator checklist (go/no-go)
→ preflight (net-read recon, feeds, PTB, clock, clean worktree)
→ explicit opt-in
→ one-shot live host start
→ discover window → READY gates
→ at most one EnterIntent lineage
→ LiveOMS → fills → active manage
→ mandatory exit before resolution
→ terminal flat
→ post-trade recon + report
→ disable live / rollback
→ post-run review
```

---

## 11. Failure and degraded-mode behavior / abort conditions

Abort immediately (no new risk) if:

- Preflight fail  
- PTB mismatch / unconfirmed  
- Clock not READY  
- Feed stale critical  
- Ack timeout / ambiguous order  
- UNKNOWN inventory  
- Kill switch  
- Daily loss/exposure breach  
- Dirty worktree (mutate path)  
- Credential role mismatch  
- Any recon disagreement  

On abort with open confirmed qty: controlled flatten attempt once; else manual
intervention path (documented). Never invent inventory.

---

## 12. Persistence and restart behavior

- One-shot: prefer **no auto-resume** after crash with pending live order —
  recon + manual continue decision
- Persist enough to reconcilable client_order_ids
- After any crash: mutations stay disabled until operator re-approves

---

## 13. Facts, metrics, and reporting

| Artifact | Contents |
|----------|----------|
| Preflight JSON | All gates |
| Live facts JSONL | Decisions, orders, fills, exits |
| Post recon | Venue vs internal |
| Operator summary | Caps, PnL realized, abort codes, scope=A |
| Review checklist | Completed post-run |

Capture metrics for later calibration (non-blocking): edge, exit family, slip
estimate vs actual, fees estimate vs actual, basis, timing.

---

## 14. Configuration ownership and units

| Key | Provisional default | Units |
|-----|---------------------|-------|
| `live.scope` | `A` | enum |
| `live.max_buy_collateral` | `5.00` | USDC |
| `live.max_positions_per_window` | `1` | count |
| `live.max_daily_loss` | tiny (freeze numerically before run) | USDC |
| `live.max_daily_notional` | tiny | USDC |
| `live.ack_timeout_ms` | freeze before run | ms |
| `z_gap.resolution_capability` | `false` | bool |
| Opt-in | required | — |

---

## 15. Test strategy

| Test | Assert |
|------|--------|
| Default OFF | No submit without opt-in |
| Cap enforcement | Risk denies above max |
| One-shot | No rollover to second window |
| Ambiguous ack | Stop |
| Dry-run | Net-read only path still available |
| Architecture | No old/r7 imports in Z-Gap path |
| CI | Never places real orders |

---

## 16. Deterministic acceptance criteria

### Go / no-go checklist (all must be YES)

1. N1–N6 evidence packs accepted  
2. Scope A confirmed; Scope B disabled  
3. Caps set and reviewed  
4. Clean worktree  
5. Preflight pass on operator host  
6. PTB confirmation path working on recent windows  
7. Kill switch tested in dry/shadow  
8. Rollback/disable procedure understood  
9. Operator present for full window  

### First-run success (engineering)

1. At most one live entry attempt lineage  
2. All orders acked or aborted cleanly  
3. Position flattened before resolution  
4. Post-trade recon: no UNKNOWN; no unexpected open orders  
5. Report complete with realized fill prices/fees when available  
6. Live disabled after run  

### Non-criteria

- Net profitable  
- Statistically significant calibration  

---

## 17. Expected deliverables

- Operator runbook (this folder)  
- Sealed tiny-live config  
- Preflight + one-shot CLI  
- First-run evidence pack  
- Post-run review notes  
- Progression criteria to limited continuous (below)

### Progression: one-shot → limited continuous

Only after **separate** acceptance:

- ≥N successful one-shots without safety incident  
- Recon always clean  
- Explicit continuous design (rollover + flat requirement)  
- New opt-in + caps  
- Still Scope A unless Scope B completed  

---

## 18. Stop conditions

- Any UNKNOWN with exposure  
- Desire to “just enable hold/redeem quickly”  
- Non-interactive submit path  
- Continuous live without separate acceptance  
- Raising caps to chase losses  

---

## 19. Remaining risks and decisions

| Item | Notes |
|------|-------|
| Exact daily loss limit | Freeze before first run |
| Deployment host | Prefer stable clock / low jitter (decision #12) |
| Interactive approval phrase | Adapt R7/old ceremony concepts; new code |
| Fee uncertainty | Bound collateral; label confirmed vs estimated |

---

## 20. Expected commit boundary

```text
N7 commit theme (when implemented):
  "Add operator-gated tiny Z-Gap live one-shot (Scope A)"

Include: CLI gates, config, runbook, tests for gates
Exclude: continuous live, Scope B redeem, push of secrets, unattended enablement
```

First real-money run is an **ops event**, not necessarily the same commit as
code enablement. Planning-only docs may ship with the N1–N7 roadmap commit.
