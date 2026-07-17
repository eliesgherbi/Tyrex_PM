# 08 — Validation report

## Checkpoints (committed, not pushed)

| Phase | Commit | Message |
|-------|--------|---------|
| R4 | `9813001465db1fd188a4e00a3c82a24fa2cb4292` | add intent risk and dry execution planning |
| R5 | `5cc1a306ee168f18df40e2107e78785d5a097364` | add shadow oms portfolio lifecycle and recovery |
| R5.1 | `6b03cc32e3d65dfdf787ce346a73dcd7b545b6d1` | stabilize shadow retry policy and unify trading host |

`.env` SHA256 (unchanged): `27210C97AE37101DE48570130BBB517E572F3EB75160B5FC4C05CF178B91F772`  
`var/` gitignored · no `old/` imports · no NautilusTrader

R6 checkpoint is created only after R6C gate completion (see below). **Not pushed.**

---

## R5.1 stabilization

**Pytest at R5.1:** 141 passed (now 166 with R6 tests)

### Host unification

- `TradingHost = ObserveHost` — single evaluate pipeline.
- `ShadowHost` overrides `_build_decision_context` / `_process_transition` only.
- `live_runner.run_live` shared by observe/shadow wrappers.

### Retry / residual

- Entry: cooldown + material book fingerprint + attempt cap (`RetryController`).
- Exit: one outstanding request; escalate on kill/market-close; `MANUAL_INTERVENTION` for residual.
- `TERMINAL` only from `FLAT`.

### Live-shadow note

Quiet windows produced FLAT/UNAVAILABLE-only signals (0 intents). Unit tests cover storm prevention. Pre-R5.1 storm (89 enter / 488 exit) is the baseline fixed by R5.1.

---

## R6A — Live adapter (fixtures)

### Official venue semantics

Documented in `Docs/Implementation/r6_venue_semantics.md` (CLOB V2).  
No `clientOrderId` on wire — correlate via returned `orderID`.  
Historical `old/` inspected for lessons; **not imported**.

### Modules

```text
src/tyrex_pm/execution/polymarket/
  auth.py, transport.py, fake_transport.py, normalize.py,
  reconciliation.py, readiness.py, live_oms.py, readonly_client.py
scripts/r6b_readonly_probe.py
tests/test_r6_*.py
```

### State machines

- Submit: `COMMAND_CREATED → SUBMITTING → VENUE_ACCEPTED | REJECTED | UNKNOWN_SUBMISSION`
- Cancel: CancelPending → Canceled; uncertain cancel reconciles; mutations off short-circuits
- `mutations_enabled=False` by default (R6) — transport submit/cancel never called

### Reconciliation / readiness

Classifications implemented; unknown external orders never auto-canceled; missing evidence fails closed.  
Readiness reasons include credentials, stream, reconcile, unknown submission, mutations disabled.

### Tests

Scripted FakeTransport scenarios + architecture/auth/reconcile/readiness.  
**Full suite: 166 passed.** Default suite makes no live mutation calls.

---

## R6B — Authenticated read-only

| Item | Result |
|------|--------|
| Credentials | Present (L2 key triple + funder); values never logged |
| submit/cancel | **Blocked** in read-only client |
| CLOB L2 GET | **Cloudflare 403 / error 1010** from this network |
| Data-API positions | HTTPError in this run |
| Reconciliation | `UNRESOLVED` + `blocks_entry=true` (missing evidence) |
| Readiness | `blocked_clob_l2_unreachable` |
| Mutations attempted | **False** |
| `.env` | Unchanged |
| Report | `var/reporting/r6/readonly_probe.json` (gitignored) |

**Blocker:** Authenticated CLOB HTTP from this environment is Cloudflare-denied. Adapter + fail-closed readiness are validated; live private reads need a non-blocked network/path before R7.

---

## R6C — Operational readiness

Full report: [`Docs/Implementation/r6c_completion_report.md`](Implementation/r6c_completion_report.md)  
Target-host handoff: [`scripts/r6c_target_host_handoff.md`](../scripts/r6c_target_host_handoff.md)

| Gate | Result |
|------|--------|
| Endpoint taxonomy | Documented (public market-data / authenticated account / public Data API) |
| Public connectivity (agent) | `/time` 200; `/book` origin-reached (404 synthetic); TLS/DNS OK |
| Authenticated L2 | Public OK; L2 GETs **401** (app auth reached, CF cleared); Data API `/positions` 200 |
| Mutation-impossible preflight | `PreflightReadClient` has no submit/cancel; no enable-mutations flag |
| Heartbeat | Official `POST /heartbeats` can cancel all opens if armed then stopped — **not called** |
| Signing dry | Synthetic V2 vectors; never sent |
| R5.1 storm | Fixture: 2123 signals → 2 enter / 2 exit / 0 dup denials / 4 cmds / FLAT |
| Host unification | One `TradingHost`; architecture tests |
| User stream | Code path ready; confirm on target host with `--user-stream-s` |

**R6C complete for code/safety gates.**

## R6D — L2 auth resolution

Full report: [`Docs/Implementation/r6d_auth_resolution.md`](Implementation/r6d_auth_resolution.md)

| Item | Result |
|------|--------|
| Root cause | `POLY_ADDRESS` was funder; must be signer EOA |
| Fix | Derive signer from `POLYMARKET_PK`; wrap `py-clob-client-v2` read-only |
| Authenticated REST | open orders / trades / balance / positions **ok** |
| User stream | connected + authenticated (idle OK); disconnect/reconnect |
| Readiness | `MUTATIONS_DISABLED` only |
| `.env` | Unchanged |
| Tests | 198 passed |

## R7 readiness verdict (post-R6D)

Financial envelope approved for a **future** R7B: max **$5** cumulative BUY, one lifecycle.  
That approval does **not** authorize submission.

## R7A — Mutation path + dry validation

Full report: [`Docs/Implementation/r7a_completion_report.md`](Implementation/r7a_completion_report.md)

| Item | Result |
|------|--------|
| Mutation protocols / spy / gated SDK transport | Implemented |
| `LiveBudgetGuard` + approval artifact + lifecycle SM | Implemented |
| Order policy | **FAK** dollar amount ≤ $5 (official V2) |
| Heartbeat | **Not called**; avoid for FAK one-shot |
| Dry lifecycle + budget/approval tests | 32 R7A tests; **230** full suite |
| `r7a-prepare --run-preflight` | Read-only; `mutations_attempted=false` |
| User stream | Ready |
| Existing positions | **4 nonzero → blocks R7B artifact** |
| Approval artifact | **Not issued** |
| `live-once` | Refuses real execution |
| `.env` | Unchanged |
| Real mutations | **None** |

**R7A complete for code/dry gates.** R7B not requested: account not flat.

## R7A.1 — Market-time / positions / fees / readiness

Full report: [`Docs/Implementation/r7a1_correction_report.md`](Implementation/r7a1_correction_report.md)

| Item | Result |
|------|--------|
| Root cause of ~1-day window | Used Gamma `startDate` (listing) as `market_start` |
| Authoritative window | Slug epoch → start; end = start+300s |
| Deadlines | Derived from `market_end` (not `now+10m`) |
| Four positions | All `RESOLVED_REDEEMABLE` (`curPrice=0`); not CLOB-flattenable |
| Selected market | Flat (no match) |
| Fees | `fd.r=0.07`, `e=1`; amount+fee ≤ $5 (e.g. 4.83+0.17) |
| Readiness | Unified `r7_readiness` includes `ACCOUNT_EXPOSURE_PRESENT` |
| Approval artifact | **Not issued** |
| Mutations / cleanup | **None** |
| Tests | **252** passed |
| `.env` | Unchanged |

**Stop after R7A.1.** No flatten, redeem, submit, cancel, or R7B.

## R7A.2 — Acknowledgment + session envelope

Full report: [`Docs/Implementation/r7a2_authorization_workflow.md`](Implementation/r7a2_authorization_workflow.md)

| Item | Result |
|------|--------|
| R7A/R7A.1 checkpoint | `f3aeaee` (not pushed) |
| R7A.2 checkpoint | `35cc52d` (not pushed) |
| Four positions acknowledged | Exact fingerprints; untouched; no redeem/sell |
| Readiness after ack | `MUTATIONS_DISABLED` + legacy `R7B_AUTHORIZATION_ABSENT` (session model; superseded for live) |
| Session draft | Two-level envelope; `user_authorization_present=false` |
| Mutations / orders | **None** |
| Tests | **270** passed |
| `.env` | Unchanged |

**Stop after R7A.2.** Awaiting explicit R7B **session** authorization (not the position ack).

**Not authorized:** real submit, cancel, wallet approval, on-chain ops, network arm token, `mutations_enabled=True`.

## R7B — Operator CLI (checkpoint)

| Item | Result |
|------|--------|
| Checkpoint | `d506ea6` (not pushed) |
| Auth model | Operator `--execute-live` (no chat/nonce ceremony) |
| Dry default | Yes; dry never mutates |
| First live run | `d632b631-…` — BUY matched, auto-SELL balance=0 → `MANUAL_INTERVENTION` |
| Incident doc | [`Docs/Implementation/r7b_first_live_incident.md`](Implementation/r7b_first_live_incident.md) |

## R7C — Settlement / reconciliation hardening

| Item | Result |
|------|--------|
| Live retest | **Forbidden this phase** |
| Hardening | `settlement.py` wait + sell readiness; lifecycle MATCHED≠CONFIRMED |
| Regression fixture | `tests/fixtures/r7b_incident_d632b631_facts.jsonl` |

## R7C.1 — Acceptance semantics

| Item | Result |
|------|--------|
| Live execution | **None** |
| Settlement finality | **CONFIRMED only** (+ conditional balance); MINED cannot sell |
| Dust | BUY 9.470587 − SELL 9.47 → **0.000587** → `FLAT_WITH_DUST` |
| Data API vs CLOB | Data API may look flat; conditional balance authoritative |
| Ack validation | Fail closed on incomplete/duplicate/disagreeing rows |
| Address roles | Proxy mode refuses signer-as-conditional-owner |
| Live worktree | Clean required for `--execute-live` |
| Read-only command | `tyrex-pm r7c-recon` |

**Stop after R7C.1.** No further live test until explicit review.

## R7D.1 — Durable acknowledgment gate

| Item | Result |
|------|--------|
| Root cause | Ack lived under disposable `var/reporting/r7/`; delete skipped gate |
| Durable path | `var/state/r7/position_acknowledgment.json` |
| Dust state | `var/state/r7/lifecycle_dust.json` (`FLAT_WITH_DUST`) |
| Gate | Mandatory dry+live; missing/invalid → BLOCKED |
| Regenerate | `tyrex-pm r7-ack-regenerate` (read-only, zero mutations) |
| Live | **Not executed** |

Full note: [`Docs/Implementation/r7d1_acknowledgment_state.md`](Implementation/r7d1_acknowledgment_state.md)

## R7D.2 — Operator handoff preparation

| Item | Result |
|------|--------|
| Ack policy | `config/r7/acknowledgment_policy.json` seals exact four identities |
| Regenerate | Cannot broaden; fifth resolved position not auto-acked |
| Residuals | `var/state/r7/lifecycle_residuals.json` (multi-record; cleanup `NONE`) |
| Auth docs | Session/nonce/verbatim marked superseded; CLI help updated |
| Live | **Not executed** (operator command prepared only) |

Full note: [`Docs/Implementation/r7d2_operator_handoff.md`](Implementation/r7d2_operator_handoff.md)

## R7E — Side-correct exit planning

| Item | Result |
|------|--------|
| Second-live incident | BUY settled; SELL FAK @ BUY limit 0.51 → no match; manual SELL @ 0.50 |
| Current state | Conditional dust `0.000587` → `FLAT_WITH_DUST`; market closed |
| Fix | Bid-side exit planner + fresh book + bounded FAK retry |
| Live retest | **Not executed** |
| Incident doc | [`Docs/Implementation/r7d2_second_live_incident.md`](Implementation/r7d2_second_live_incident.md) |

## R7F — Exit planner acceptance + book rehearsal

| Item | Result |
|------|--------|
| Operator live proof | `55fd9a76` BUY@0.51 → SELL@0.50 → inventory **`FLAT_WITH_DUST`** |
| Policy | `r7_lifecycle_policy.py` exact values |
| Rehearsal | `scripts/r7f_exit_rehearsal.py` — zero mutations |
| Residuals | 3 distinct `FLAT_WITH_DUST` records; ack gate ok |
| Agent live | **Not executed** |
| Doc | [`r7f_exit_planner_acceptance.md`](Implementation/r7f_exit_planner_acceptance.md) · [`r7f_operator_runbook.md`](Implementation/r7f_operator_runbook.md) |

## R8 — R7 closure + framework acceptance

| Item | Result |
|------|--------|
| Further R7 live | **Not required** (third live complete) |
| Terminal semantics | Dust ⇒ `FLAT_WITH_DUST`; exact zero ⇒ `FLAT` |
| Exit floors | Documented; NORMAL≠EMERGENCY (slippage); ≠ legacy REJECT |
| Account recon | Ack=4; 3 dust; open orders=0; cleanup `NONE` |
| Framework matrix | [`r8_framework_acceptance.md`](Implementation/r8_framework_acceptance.md) |
| Success evidence | [`r7_successful_live_acceptance.md`](Implementation/r7_successful_live_acceptance.md) |
| Z-Gap | **Not implemented** this phase |
