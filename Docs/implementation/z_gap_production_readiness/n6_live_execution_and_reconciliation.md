# N6 — Live execution and reconciliation

**Status:** **ACCEPTED** — Gates 1–3 PASS (see `n6_acceptance_report.md`).  
Gate 3 authenticated read-only: `LIVE_READONLY_OK` on operator host  
(`var/reporting/n6/readonly_manual/`). Mutations remain disabled; N7 not enabled.  
**Document:** `Docs/implementation/z_gap_production_readiness/n6_live_execution_and_reconciliation.md`  
**Depends on:** N5 real-input SHADOW acceptance + existing R7 LiveOMS/recon evidence (concepts only)  
**Unblocks:** N7 tiny operator-controlled live planning (mutations still off until N7)

---

## 1. Objective

Plan (and later implement) the **generic** live execution and reconciliation
capability required by Z-Gap — without enabling operator live trading in N6
itself until N7 explicitly opts in.

N6 separates:

| Truth domain | Contents |
|--------------|----------|
| **Execution truth** | Acks, actual fills, fees, balances, positions, proceeds |
| **Resolution truth** | Official winning outcome, finality, payout, optional redemption |

and presents two live scopes (A recommended first; B full strategy).

---

## 2. Why the milestone exists

R7 validated tiny-live for ReferenceMomentum via phase-specific `runtime/r7*`
paths. Z-Gap must not import those packages. Generic live capability must be
composed for any strategy (including Z-Gap) with fail-closed auth, user-stream
fills, reconciliation, and restart semantics — before any tiny live money.

---

## 3. Scope

### Execution capability checklist

| Topic | Plan |
|-------|------|
| Auth / secret boundaries | Credentials only in `.env` / OS env; never logged; signer vs funder roles |
| Public market channel | Existing CLOB market WS (books) |
| Authenticated user/order channel | User WS + L2 REST; distinct from public |
| Order submission / ack | LiveOMS submit → insert ack; timeouts |
| Marketable limit semantics | Planner-owned; FAK-style as validated in R7 unless N6 freezes alternate |
| Order IDs / client IDs | Idempotent client order id; venue order id mapping |
| Partial fills | Track confirmed qty only; strategy still prefers full exit |
| Cancellations | Explicit cancel; never heartbeat-cancel-all in safe paths |
| Actual average fill price | From fill evidence — **not** submitted limit |
| Actual venue fees | When reported; else estimated labeled |
| Rejected / ambiguous orders | Fail closed; stop automated action |
| Open-order reconciliation | REST vs OrderStore |
| Balance reconciliation | Funder conditional balance authoritative for sellability |
| Portfolio / inventory reconciliation | Internal vs venue; UNKNOWN on conflict |
| Restart with pending orders | Reconcile before new risk |
| Retry / idempotency | **Verify** venue client-order support; else framework lineage (see below) |
| Kill switch | Shared risk; operator command |
| Stale-near-expiry | Scope A timing ladder: flatten/block — no silent hold |
| Actual realized P&L | Confirmed fills/fees only |
| Audit facts | Full order lifecycle |
| Operator commands | Preflight, recon, kill, abort |
| Fail-closed startup | Dirty worktree / failed recon / missing readiness → no submit |
| Authenticated read-only acceptance | **Required** before N7 (mutations still disabled) |

### Resolution vs execution (must not conflate)

```text
Execution truth ≠ Resolution truth
Submitted price ≠ Fill price
Binance ≠ Chainlink settlement
Simulated payout ≠ Redeemed proceeds
```

### Live scopes

#### Scope A — recommended first live (N7 default)

| Property | Value |
|----------|-------|
| Resolution hold | **Disabled** |
| Monetization | Mandatory pre-resolution exit via **timing ladder** (see initiative README / N7) |
| Redeem | **Not required** |
| Lifecycle | Entry → exit → flat before event end |
| Extra work vs N5 | LiveOMS + user fills + recon + ack/timeouts + auth read-only gate |

Scope A deadlines (relative to authoritative `event_end`; numeric freeze before N7):

last allowed entry · discretionary exit cutoff · mandatory flatten start ·
ack timeout · cancel/recon budget · final residual/operator deadline ·
event-end safety buffer.

Late entry is **skipped**. Scope A never silently becomes hold-to-resolution.

#### Scope B — full live strategy

| Property | Value |
|----------|-------|
| Resolution hold | Enabled when capability + evidence ready |
| Pending capital | Must account inventory while RESOLUTION_PENDING |
| Final confirmed resolution evidence | Real feed/API — not fixture |
| Payout / redeem | Real redeem capability (today **unsupported/forbidden** in framework) |
| Idempotent redemption + recon | Required |
| Additional work | Redeem transport, finality detector, capital lock accounting, PONR ops, residual redeem policy — **large** |

**Do not silently choose Scope B.** N7 must start as Scope A unless Scope B
work is explicitly completed and accepted.

---

## 4. Explicit non-goals (for the N6 planning/design phase)

- No enabling `--execute-live` for Z-Gap in N6 planning commit
- No implementing redeem in the planning commit
- No importing `old/` or depending on Z-Gap → `runtime/r7*`
- No continuous unattended live product
- No profitability gate

When N6 is later implemented: still no Z-Gap money until N7.

---

## 5. Dependencies and entry criteria

| Criterion | Notes |
|-----------|-------|
| N5 acceptance | Real-input simulated lifecycle trusted |
| Existing LiveOMS / recon / auth | Reuse under `execution/polymarket/` |
| R7 evidence | Concepts only — reimplement composition generically |
| Polymarket capability matrix | Redeem still unsupported → Scope B blocked until built |
| Decision freeze: Scope A vs B for first live | Master decision table #8 |

---

## 6. Decisions that must already be frozen

- Submitted price ≠ execution truth
- UNKNOWN blocks action + recon
- Live capability operator-enabled, default OFF
- No Z-Gap-specific LiveOMS
- Scope A vs B choice before N7
- Initial order type / marketable limit policy (#9)

---

## 7. Responsibility / module ownership

| Concern | Owner |
|---------|-------|
| Auth / transports | `execution/polymarket/*` |
| LiveOMS | `execution/polymarket/live_oms.py` |
| Recon | `execution/polymarket/reconciliation.py` (+ generic host wiring) |
| User stream | `user_stream_readonly` / mutation lifecycle modules |
| Risk kill / caps | RiskEngine policies |
| Planner qty/price/style | planning |
| Portfolio / lifecycle | portfolio / lifecycle |
| Generic live host composition | `runtime/` **non-r7** module (new or generalized) |
| Z-Gap | Intents only |
| Ops CLI | `application/cli.py` |

**Forbidden:** `strategies/z_gap` importing `execution.polymarket` or `runtime/r7*`.

---

## 8. Contracts, ports, and data structures to add or evolve

| Contract | Evolution |
|----------|-----------|
| `OMS` protocol | Already; ensure LiveOMS meets it without strategy knowledge |
| Execution events | Ack, partial fill, cancel, reject, ambiguous |
| Recon report | Open orders, balances, positions, disagreement class |
| Live readiness | Feed + auth + clock + inventory flatness + kill clear |
| Idempotency | **OPEN audit:** verify Polymarket client_order_id support; else framework lineage |
| Framework lineage | `intent_id` → `plan_id` → request fingerprint → submission attempt ID → venue order ID when known |
| Resolution evidence (Scope B) | Real finality port — distinct from execution fills |
| Redeem port (Scope B) | New; absent today |
| Hard collateral cap | Fee-inclusive max; **SKIP** if min valid order exceeds cap (never auto-raise) |

---

## 9. Expected files / modules affected (when implemented)

```text
src/tyrex_pm/execution/polymarket/live_oms.py
src/tyrex_pm/execution/polymarket/reconciliation.py
src/tyrex_pm/execution/polymarket/mutation_*.py
src/tyrex_pm/execution/polymarket/user_stream*.py
src/tyrex_pm/runtime/          # generic live host (NOT r7* imports into z_gap)
src/tyrex_pm/risk/policies.py  # live mode gates, generic
src/tyrex_pm/application/cli.py
tests/                         # fake transport live lifecycle
Docs/latest/integrations/polymarket.md
Docs/latest/how_to/reconciliation_and_recovery.md
```

Promote useful R7 mechanisms into generic modules; leave `runtime/r7*` as
historical phase code.

---

## 10. End-to-end data or control flow

### Scope A

```text
Authenticated read-only preflight (mutations OFF) — must pass before any mutate path
→ Preflight recon (flat / known)
→ feeds READY + PTB attested+locked + clock READY
→ sealed evaluate → EnterIntent (only if before last-entry deadline)
→ Risk → Plan (hard cap; SKIP if min valid > cap)
→ LiveOMS.submit (lineage IDs)
→ ack / user-stream fills → Portfolio (confirmed qty)
→ active evaluate → ExitIntent within timing ladder
→ LiveOMS exit → confirmed flat before residual deadline
→ post-trade recon → report
```

### Idempotency and ambiguity

1. Verify whether Polymarket supports caller-controlled idempotent client order IDs.  
2. If unavailable/insufficient, use framework lineage above.  
3. Ack timeout → **AMBIGUOUS** state.  
4. Ambiguity triggers user-stream/REST reconciliation.  
5. Never send a fresh entry merely because the original response was lost.  
6. Retry semantics bounded and evidence-aware.

### Scope B additions

```text
… ACTIVE → HoldToResolutionIntent
→ RESOLUTION_PENDING (PONR rules)
→ resolution finality evidence
→ redeem (idempotent)
→ payout applied → recon → FLAT
```

---

## 11. Failure and degraded-mode behavior

| Exposure state | Failure behavior |
|----------------|------------------|
| FLAT | Block new exposure |
| ACTIVE with confirmed inventory | Continue risk management; seek safe exit |
| Inventory UNKNOWN | Reconcile; never guess quantity |
| Entry order ambiguous | Stop new actions; recon before retry |
| Exit partially filled | Manage only confirmed residual quantity |
| Resolution committed | Remain pending; do not fabricate a sell (Scope B) |

| Failure | Behavior |
|---------|----------|
| Ack timeout | AMBIGUOUS; recon; no second entry |
| Partial entry | Manage confirmed qty only |
| Reject | Record; consume or retry per evidence-aware policy |
| Balance disagreement | UNKNOWN; block new risk |
| User stream gap | REST recon before new actions |
| Kill | Flatten confirmed qty only (Scope A) via bounded exit ladder |
| Stale near expiry | Mandatory flatten path; no silent hold |
| PTB/basis degrade while ACTIVE | Do not prevent exit management |
| Min valid order > hard cap | SKIP; never auto-raise cap |

---

## 12. Persistence and restart behavior

- Persist orders, fills, lifecycle, live mode flags, client_order_ids
- Restart: **recon first** → classify pending/open → resume exit or block
- Never assume flat on missing data
- Config fingerprint mismatch → refuse live resume

---

## 13. Facts, metrics, and reporting

| Family | Includes |
|--------|----------|
| Order lifecycle | submit, ack, reject, cancel, fill |
| Fee truth | estimated vs confirmed |
| Recon | diffs, UNKNOWN reasons |
| PnL | realized from fills only when confirmed |
| Operator | preflight, kill, abort |
| Scope tag | `live_scope=A|B` |

---

## 14. Configuration ownership and units

| Key | Owner | Units |
|-----|-------|-------|
| `live.enabled` | runtime | bool default false |
| `live.mutations_enabled` | LiveOMS | bool default false |
| `live.ack_timeout_ms` | runtime | ms |
| `live.max_order_notional` | risk | USDC |
| `live.order_style` | planning | e.g. FAK marketable limit |
| `live.scope` | runtime | `A` \| `B` |
| Credentials | env only | — |

---

## 15. Test strategy

| Layer | Approach |
|-------|----------|
| Fake transport | Full Scope A lifecycle without network |
| Ambiguous ack | Stop + recon fixtures |
| Restart pending | Recovery tests |
| Architecture | No z_gap → r7* / execution.polymarket |
| Recon | Synthetic venue disagreement → UNKNOWN |
| Cap vs min order | SKIP when min valid > cap |
| Idempotency | Lost-ack does not create duplicate entry lineage |
| Scope B | Tests only after redeem port exists; otherwise document blocked |

### Authenticated read-only acceptance (mutation-disabled) — required before N7

Real-account, **no order submission**:

1. Authenticate successfully.  
2. Validate signer/funder roles.  
3. Connect to authenticated user/order data.  
4. Read balances.  
5. Read token inventory.  
6. Read allowances if applicable.  
7. Read open orders.  
8. Reconcile venue state with internal state.  
9. Restart and reconcile again.  
10. Prove mutations/order submission remain disabled.  
11. Ensure credentials and secrets are never logged.

N7 must **not** be the first time real authentication, user-channel connectivity,
or account reconciliation is exercised.

Dry-run net-read may reuse preflight patterns; no CI live money.

---

## 16. Deterministic acceptance criteria (implementation phase)

1. Generic live host can run Scope A against fake transport end-to-end.
2. Mutations remain default OFF; explicit enable required.
3. Submitted price never used as fill truth in Portfolio.
4. Recon detects open-order and balance disagreements.
5. Restart with pending order fails closed or resumes correctly (tested).
6. Kill switch prevents new exposure and drives bounded exit of confirmed qty (Scope A).
7. Authenticated read-only acceptance completed successfully.
8. Venue idempotency capability audited and documented (supported or framework lineage).
9. Scope B checklist explicitly incomplete until redeem/finality done.
10. Z-Gap still uses sealed decision path; no venue fields on intents.
11. Pytest green; no Z-Gap live money.

---

## 17. Expected deliverables

- This design README (now)
- Later: generic live composition modules + tests
- Scope A/B checklist artifact
- Mapping document: R7 concepts → generic owners (no dependency)

---

## 18. Stop conditions

- Attempt to ship Z-Gap live by importing `runtime/r7*`
- Scope B enabled without redeem/finality
- Heartbeat-cancel-all considered “safe”
- Ambiguous orders auto-retried as new entry lineage

---

## 19. Remaining risks and decisions

| Decision | Recommendation |
|----------|----------------|
| First live scope | **Scope A** |
| Order type | Marketable limit / FAK-style per R7-validated semantics |
| Ack timeout | Fail closed → AMBIGUOUS (numeric OPEN) |
| Idempotency | **OPEN** — verify venue; else framework lineage |
| Auth read-only gate | Required before N7 |
| Hard cap | SKIP if min valid > cap |
| Redeem ownership | Framework execution/settlement — not strategy (Scope B) |
| Promotion of R7 code | Copy/adapt concepts into generic modules |

---

## 20. Expected commit boundary

```text
N6 commit theme (when implemented):
  "Add generic live OMS composition and reconciliation for strategies"

Include: generic runtime live wiring, tests, docs
Exclude: Z-Gap operator enablement (N7), Scope B redeem unless explicitly scoped,
         any forced push / live money run
```

Planning-only updates for this milestone may ship under `z_gap_production_readiness/`.
