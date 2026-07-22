# N7 — Tiny operator-controlled live

**Status:** Simplified operator one-shot — **no authorization ceremony**.  
Operator runs `python tools/n7_live/run_n7_live_oneshot.py --live` (invocation = authorization).  
Fee-inclusive entry debit ≤ $5.00. Scope A only.  
**Document:** `Docs/implementation/z_gap_production_readiness/n7_tiny_operator_live.md`  
**Depends on:** N1–N6 accepted evidence  
**Unblocks:** Operator-executed tiny live one-shot / later continuous (separate)

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
| Fixed tiny max order value | Provisional **$5 fee-inclusive hard maximum** (not a target) |
| Cap vs venue minimum | If min valid fee-inclusive order > cap → **SKIP** (never auto-raise) |
| One position per window | Existing Z-Gap policy |
| Max daily exposure/loss | Configured hard caps (OPEN numeric freeze) |
| No same-window reverse/re-entry | Existing policy |
| Kill switch | Operator + risk |
| Preflight | Includes **N6 authenticated read-only** gates + balance/inventory recon |
| Feed / PTB / time readiness | Attested+locked PTB; TimeAuthority READY |
| Scope A timing ladder | Frozen numerics relative to `event_end` (below) |
| Order ack timeout | Ambiguous → stop / recon |
| Ambiguous-order stop | No automated guess; no fresh entry on lost ack |
| Bounded exit ladder | Ack-aware retries + recon; not a single blind flatten |
| Post-trade recon | Mandatory |
| Operator report | Full evidence pack |
| Rollback / disable | Documented procedure |
| No unattended continuous | Until separate acceptance |

### Scope A timing ladder (freeze before N7)

All relative to authoritative `event_end` (values from N1/N4 measured latency + safety margin):

| Deadline | Behavior |
|----------|----------|
| Last allowed entry | Skip late entry |
| Discretionary exit cutoff | Last rich/thesis discretionary sell window |
| Mandatory flatten start | Begin forced exit |
| Order ack timeout | AMBIGUOUS if exceeded |
| Cancel / recon budget | Reserved for cancel+reconcile |
| Final residual / operator deadline | Hard stop; escalate to operator |
| Event-end safety buffer | Never silent hold-to-resolution |

### Bounded exit ladder (replaces “one flatten attempt”)

On abort or mandatory flatten with confirmed inventory:

1. Bounded, acknowledgement-aware exit attempts.  
2. Reconciliation before each retry.  
3. Never sell more than confirmed residual quantity.  
4. Explicit retry count / time budget (decision #25).  
5. Cancel/replace only when prior order state is known.  
6. Hard stop at final residual/operator deadline.  
7. Operator escalation for unresolved exposure.  
8. Complete audit facts.

**Never** perform blind repeated sells.

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
| N1 | PTB source proof + latency + lateness inputs for timing ladder |
| N2 | Adapter reliability (reconnect/heartbeat); clock provider |
| N3 | PTB axes + causal basis; attested+locked entry rule |
| N4 | Real OBSERVE engineering sample; EWMA/prep policies |
| N5 | Real SHADOW scenarios; fill model limitations documented |
| N6 | Fake-transport Scope A + **authenticated read-only** acceptance; idempotency audit |

Additional:

- Scope A timing ladder numerics frozen
- Bounded exit retry budget frozen
- Clean git worktree for mutate path
- Credentials roles validated on operator host
- Clock sync acceptable on deployment host
- Operator available for entire one-shot window

---

## 6. Decisions that must already be frozen

1. Scope A first (#8)  
2. Order type / marketable limit (#9)  
3. Tiny hard-cap / daily limits / min-order SKIP (#10, #24)  
4. One-shot operator workflow (#11)  
5. Deployment / clock sync expectations (#12)  
6. Venue idempotency audit result (#21)  
7. Scope A timing ladder (#23)  
8. Bounded exit retry budget (#25)  
9. Kill and ambiguous-order stop behavior (N6)  
10. PTB attested+locked required for live entry (N3)  
11. N6 authenticated read-only gate passed (#22)

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
Docs/implementation/z_gap_production_readiness/  # runbook + evidence
tests/                         # gates, opt-in, caps (no live money in CI)
```

---

## 10. End-to-end data or control flow

```text
Operator checklist (go/no-go)
→ N6 authenticated read-only preflight (mutations OFF) already accepted
→ preflight (recon, feeds, attested+locked PTB, clock, clean worktree, timing ladder loaded)
→ explicit opt-in
→ one-shot live host start
→ discover window → READY gates
→ at most one EnterIntent lineage (skip if past last-entry or min order > hard cap)
→ LiveOMS → fills → active manage within discretionary window
→ mandatory flatten start → bounded exit ladder
→ terminal flat before residual/operator deadline
→ post-trade recon + report
→ disable live / rollback
→ post-run review
```

---

## 11. Failure and degraded-mode behavior / abort conditions

| Exposure state | Failure behavior |
|----------------|------------------|
| FLAT | Block new exposure |
| ACTIVE with confirmed inventory | Bounded exit ladder; seek safe exit |
| Inventory UNKNOWN | Reconcile; never guess quantity |
| Entry order ambiguous | Stop new actions; recon; no fresh entry |
| Exit partially filled | Manage only confirmed residual |
| Resolution committed | N/A in Scope A (hold disabled) |

Abort immediately (no new risk) if:

- Preflight fail (including N6 auth read-only not previously accepted)  
- PTB not attested+locked / mismatch  
- Clock not READY  
- Feed stale critical while FLAT (while ACTIVE → exit ladder)  
- Ack timeout / ambiguous order  
- UNKNOWN inventory  
- Kill switch  
- Daily loss/exposure breach  
- Dirty worktree (mutate path)  
- Credential role mismatch  
- Any recon disagreement  
- Past residual/operator deadline without flat  

On abort with open confirmed qty: execute **bounded exit ladder** (not a single
blind flatten). Escalate to operator if unresolved at final deadline. Never invent inventory.

---

## 12. Persistence and restart behavior

- One-shot: prefer **no auto-resume** after crash with pending live order —
  recon + manual continue decision
- Persist enough for reconcilable lineage IDs (`intent_id` / attempt / venue id)
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
| `live.max_buy_collateral` | `5.00` **hard max** | USDC |
| `live.skip_if_min_exceeds_cap` | `true` | bool |
| `live.max_positions_per_window` | `1` | count |
| `live.max_daily_loss` | tiny (freeze numerically before run) | USDC |
| `live.max_daily_notional` | tiny | USDC |
| `live.ack_timeout_ms` | freeze before run | ms |
| `live.timing.*` | Scope A ladder offsets | s before `event_end` |
| `live.exit_retry.*` | count / budget | count / ms |
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

1. N1–N6 evidence packs accepted (including N6 authenticated read-only)  
2. Scope A confirmed; Scope B disabled  
3. Timing ladder + exit retry budget frozen  
4. Caps set as hard maxima; min-order SKIP behavior confirmed  
5. Clean worktree  
6. Preflight pass on operator host  
7. PTB attestation path working on recent windows  
8. Kill switch tested in dry/shadow  
9. Rollback/disable procedure understood  
10. Operator present for full window  

### First-run success (engineering)

1. At most one live entry attempt lineage (or intentional SKIP for late/min-cap)  
2. All orders acked or aborted cleanly via bounded exit ladder  
3. Position flattened before residual/operator deadline (no silent hold)  
4. Post-trade recon: no UNKNOWN; no unexpected open orders  
5. Report complete with realized fill prices/fees when available  
6. Live disabled after run  

### Non-criteria

- Net profitable  
- Statistically significant calibration  

---

## 17. Expected deliverables

- Operator runbook (this initiative / document)  
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
| Exact daily loss / notional | **FROZEN** at $5 / $5 (N7A) |
| Timing ladder numerics | **FROZEN** in `n7_timing.py` / `config/n7_tiny_live.json` |
| Exit retry budget | **FROZEN** 3 attempts / 60s |
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
