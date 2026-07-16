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

**R6C complete for code/safety gates.** Operational account observation still requires sanitized target-host artifact if agent L2 remains CF-blocked.

## R7 readiness verdict

**Do not begin R7** until:

1. Sanitized target-host `live_preflight` evidence reviewed (authenticated reads + user stream + clean reconcile).  
2. Explicit tiny-live authorization is given.  
3. Heartbeat supervisor separately approved (still unresolved).  

**Not authorized:** real submit, cancel, wallet approval, on-chain ops, `mutations_enabled=True`.

**Stop after R6C.**
