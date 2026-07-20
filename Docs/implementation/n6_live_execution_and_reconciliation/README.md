# N6 — Live execution and reconciliation

**Status:** planned (design only — do not implement or enable live in this milestone’s planning commit)  
**Milestone folder:** `Docs/implementation/n6_live_execution_and_reconciliation/`  
**Depends on:** N5 real-input SHADOW acceptance + existing R7 LiveOMS/recon evidence (concepts only)  
**Unblocks:** N7 tiny operator-controlled live

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
| Retry / idempotency | Bounded retry; no duplicate entry lineage |
| Kill switch | Shared risk; operator command |
| Stale-near-expiry | Fail-closed flatten or block |
| Actual realized P&L | Confirmed fills/fees only |
| Audit facts | Full order lifecycle |
| Operator commands | Preflight, recon, kill, abort |
| Fail-closed startup | Dirty worktree / failed recon / missing readiness → no submit |

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
| Monetization | Mandatory pre-resolution exit |
| Redeem | **Not required** |
| Lifecycle | Entry → exit → flat before event end |
| Extra work vs N5 | LiveOMS + user fills + recon + ack/timeouts |

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
| Idempotency keys | client_order_id namespace per strategy/window |
| Resolution evidence (Scope B) | Real finality port — distinct from execution fills |
| Redeem port (Scope B) | New; absent today |

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
Preflight recon (flat / known)
→ feeds READY + PTB confirmed + clock READY
→ sealed evaluate → EnterIntent
→ Risk → Plan → LiveOMS.submit
→ ack / user-stream fills → Portfolio (confirmed qty)
→ active evaluate → ExitIntent
→ LiveOMS exit → confirmed flat
→ post-trade recon → report
```

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

| Failure | Behavior |
|---------|----------|
| Ack timeout | Ambiguous-order stop; recon; no second entry |
| Partial entry | Manage confirmed qty only; no fantasy inventory |
| Reject | Record; consume or retry per policy; fail closed on ambiguity |
| Balance disagreement | UNKNOWN; block |
| User stream gap | REST recon before new actions |
| Kill | Flatten confirmed qty only (Scope A); Scope B after PONR follows F5 hold rules |
| Stale near expiry | Flatten/block — no silent hold (Scope A) |

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
| Scope B | Tests only after redeem port exists; otherwise document blocked |

Dry-run net-read may reuse preflight patterns; no CI live money.

---

## 16. Deterministic acceptance criteria (implementation phase)

1. Generic live host can run Scope A against fake transport end-to-end.
2. Mutations remain default OFF; explicit enable required.
3. Submitted price never used as fill truth in Portfolio.
4. Recon detects open-order and balance disagreements.
5. Restart with pending order fails closed or resumes correctly (tested).
6. Kill switch prevents new exposure and flattens confirmed qty (Scope A).
7. Scope B checklist explicitly incomplete until redeem/finality done.
8. Z-Gap still uses sealed decision path; no venue fields on intents.
9. Pytest green; no Z-Gap live money.

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
| Ack timeout | Fail closed (numeric open) |
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

Planning-only updates to this folder may ship with the N1–N7 roadmap commit.
