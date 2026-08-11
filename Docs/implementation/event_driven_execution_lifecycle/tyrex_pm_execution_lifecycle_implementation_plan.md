# Tyrex_PM event-driven execution lifecycle — implementation plan

**Status:** Phase 9 **BLOCKED** (Level 5 duration gate not executed) — see `PHASE_9_CURRENT_STATUS.md`. Historical outcome labels in FINAL_IMPLEMENTATION_REPORT.md are superseded for current status; `tiny_live_admitted` remains false.

**Plan version:** 2.0 — ten-phase executable plan

**Rebaseline date:** 2026-08-06

**Implementation completion date:** 2026-08-07

**Branch:** `rest_project`

**Rebaseline HEAD:** `28c4d804f0c2927579c35743bfcedb807330d768`

**Final suite (deterministic, post-remediation):** 1014 passed — see `REMEDIATION_IMPLEMENTATION_REPORT.md` and `remediation_evidence/full_suite_remediation.txt`. Real venue mutations = 0; tiny-live experiment not executed; `tiny_live_admitted` remains false. Phase 9 overall remains **BLOCKED** (Level 5).

**Prior incident:** `yaml_live_20260731T143127Z` — operator liquidation completed; software defect addressed by Phases 2–8 (pending Level 5 operator evidence before Phase 10 admission)

**Scope:** Complete Strategy → Intent → Risk → Planner → OMS → execution evidence → exposure → settlement → exit → reconciliation for the N7 Polymarket one-shot lifecycle

---

## 1. Purpose and how to use this plan

This is the primary implementation plan for repairing the N7 live execution lifecycle. It is written so that a developer can implement it sequentially without inventing architecture, weakening safety gates, or changing Z-Gap strategy economics.

The ten phases are mandatory and ordered:

```text
Foundation
  1. Repository consistency

Terminal safety
  2. Execution obligations
  3. Complete submission evidence

Live event wiring
  4. Continuous authenticated user stream
  5. Exposure-driven exit supervisor
  6. Shared settlement and sellability

Freshness and authority
  7. Continuous feeds and pre-submit revalidation
  8. Baseline-aware terminal reconciliation

Acceptance
  9. Non-live validation ladder
 10. One monitored tiny-live experiment
```

### 1.1 Execution rules for developers

1. Implement phases in order. Do not begin a phase until the prior phase gate is recorded as PASS.
2. Keep every commit testable and keep real venue mutation disabled through Phase 9.
3. Do not change Z-Gap thresholds, model formulas, risk caps, or strategy decisions while repairing lifecycle wiring.
4. Reuse existing owners before creating new ones. New components are allowed only where the current architecture has no owner.
5. Never use `Portfolio.is_flat()` as authoritative terminal evidence after a mutation attempt.
6. Do not enable an operator live path through a configuration toggle. Phase 10 admission requires the Phase 9 evidence package and an explicit operator action.
7. If venue or SDK semantics are uncertain, stop that slice, preserve the uncertainty in a test/fixture, and resolve it before continuing.
8. After each phase, update this document or a companion completion report with commit, tests, evidence, deviations, and unresolved risks.

### 1.2 Required phase completion record

Every phase completion report must contain:

```text
phase:
starting_head:
ending_head:
files_changed:
contracts_added_or_changed:
tests_added:
targeted_test_result:
full_suite_result:
venue_mutations_attempted: 0   # Phases 1–9
evidence_artifacts:
deviations_from_plan:
remaining_blockers:
gate: PASS | FAIL
```

No phase is complete when its full-suite result is unknown, its required evidence is missing, or its gate is FAIL.

---

## 2. Executive verdict

```text
REUSE_FIRST                         = YES
FRAMEWORK_REWRITE_REQUIRED          = NO
N7_BYPASSES_EXISTING_LIFECYCLE      = YES (PROVEN)
R7_SETTLEMENT_MODEL_REUSABLE        = YES
FALSE_PASS_POSSIBLE_AT CURRENT HEAD = YES (PROVEN)
NEXT_TINY_LIVE_SAFE AT CURRENT HEAD = NO
FIRST IMPLEMENTATION PHASE          = Repository consistency
FIRST LIFECYCLE PHASE               = Execution obligations
LIVE REMAINS BLOCKED UNTIL           = Phase 10 admission
```

The implementation objective is:

> Preserve complete Polymarket submission evidence, feed HTTP and authenticated user-stream execution observations into one idempotent OMS path, supervise exposure from first match awareness, wait for confirmed and sellable inventory before SELL, keep market feeds alive through dispatch, and forbid terminal success unless authenticated reconciliation proves return to the pre-run baseline.

This plan repairs execution reachability and authority. It does not change the Z-Gap hypothesis or attempt to prove profitability.

---

## 3. Scope boundaries

### 3.1 In scope

- YAML/configuration, test, CLI, and documentation consistency required for a clean baseline
- Complete submission result normalization
- Per-order and per-session execution obligations
- Long-running authenticated Polymarket user stream for a live session
- HTTP/WebSocket ordering and idempotency
- Matched, confirmed, and sellable quantity projections
- Exposure-driven exit supervision
- R7 settlement/sellability reuse
- Continuous market feeds through submit and exit
- Immediate pre-submit revalidation
- Read-only recovery and baseline-aware reconciliation
- Restart reconstruction and non-PASS uncertainty
- Deterministic, fixture, shadow, read-only, and one monitored tiny-live acceptance

### 3.2 Explicitly out of scope

- Changing Z-Gap `theta`, `z`, `tau`, thesis, time-exit, or fee-model parameters
- Increasing the fee-inclusive `$5.00` entry cap
- Multiple simultaneous entry lineages
- Same-window re-entry or reversal
- Continuous multi-window autonomous live trading
- Scope B or hold-to-resolution live execution
- On-chain redeem, transfer, allowance mutation, or cleanup automation
- Strategy profitability claims or calibration approval
- Distributed event buses or multi-process architecture
- Rewriting the R7 path as a prerequisite for N7 admission

---

## 4. Verified current defects at the rebaseline

The following findings were rechecked against current source and remain the reason live is blocked:

| Finding | Current evidence | Required phase |
|---|---|---:|
| N7 stops compose/feeds after capturing an entry candidate | `runtime/n7_live_session.py` sets `stop_requested`; compose exits before submission | 7 |
| N7 uses fixed `asyncio.sleep(3.0)` as execution control | `runtime/n7_live_session.py` | 5 |
| Empty local portfolio can be considered flat | `Portfolio.is_flat()` uses `all([]) == True` | 2, 8 |
| N7 terminal PASS ignores unresolved reconciliation | outcome is selected from local economics/flatness while `recon_blocks_entry` is only reported | 2, 8 |
| Submission result drops important SDK evidence | normalized result does not retain all trade IDs/amounts | 3 |
| Authenticated user stream is a preflight probe, not a live supervisor | `user_stream_readonly.py` is not wired through the mutation session | 4 |
| Existing confirmed-trade ingestion is not driven by production N7 events | `N6LiveHost.ingest_confirmed_trade` is mostly reached by tests/R7 patterns | 3, 4, 6 |
| R7 settlement wait exists but is not used by N7 | `wait_for_entry_settlement` is wired in R7, not the N7 session | 6 |
| N7 recovery transport is incomplete for authoritative reconstruction | mutation and read-only responsibilities are not composed for the session | 8 |
| Repository baseline is inconsistent | deleted JSON configs remain referenced by CLI/docs/tests; latest recorded full suite has 48 failures | 1 |

No phase may treat these findings as already solved without new tests and evidence.

---

## 5. Target architecture and authority model

### 5.1 Target runtime flow

```text
EnterIntent
→ RiskEngine
→ ExecutionPlanner
→ pre-submit revalidation
→ SubmissionLineage
→ LiveOMS.submit
→ submission obligation persisted
→ SdkMutationTransport
→ complete SubmitOrderResult
→ LiveOMS applies order/match evidence
↔ UserStreamLive applies authenticated order/trade evidence
→ OrderStore + matched-exposure projection
→ ExitSupervisor wakes when matched exposure > 0
→ settlement projection waits for CONFIRMED
→ sellability projection checks conditional balance
→ FlattenIntent / exit planner / LiveOMS
→ read-only reconciliation
→ NO_FILL_CONFIRMED | FLAT_CONFIRMED | explicit non-PASS
```

### 5.2 Quantity model — do not conflate these axes

| Quantity | Meaning | Owner | Effect |
|---|---|---|---|
| Requested quantity | Quantity in the submitted plan | Plan/order record | No exposure; never sellable |
| Matched quantity | Venue reports economic execution/match | `OrderStore` high-water mark + matched-exposure projection | Starts protection and exit supervision |
| Confirmed quantity | Trade reached required settlement status | settlement projection / confirmed fill application | May update confirmed portfolio inventory |
| Sellable quantity | Confirmed inventory supported by funder conditional balance | sellability projection | Maximum quantity allowed in a SELL |
| Exited quantity | Confirmed exit execution | fills/portfolio/reconciliation | Reduces strategy-owned exposure |

Mandatory invariant:

```text
requested_qty is not inventory
matched_qty starts supervision but is not automatically sellable
confirmed_qty may still exceed sellable_qty temporarily
SELL qty <= min(confirmed_qty, conditional_balance, remaining_owned_qty)
```

A MATCHED HTTP or stream observation must not be passed through a helper that silently treats it as CONFIRMED portfolio inventory.

### 5.3 Source-of-truth hierarchy

The hierarchy depends on the question being answered:

| Question | Primary source | Recovery/cross-check |
|---|---|---|
| Was the request accepted? | HTTP submission result | REST order lookup |
| Has quantity matched? | authenticated user stream cumulative match/trades | HTTP response evidence + REST orders/trades |
| Was an execution applied locally? | single-writer OMS + `FillLedger` idempotency | reconciliation |
| What economic exposure requires supervision? | matched-exposure projection bound to lineage | venue cumulative matched quantity |
| What inventory is confirmed? | confirmed trade settlement evidence | REST trades |
| What quantity is sellable? | funder conditional-token balance intersected with confirmed owned quantity | repeated authenticated balance read |
| Is the session terminal? | baseline-aware reconciliation: orders + trades + balances + obligations | local stores must agree; disagreement is non-PASS |

Venue positions/balances are not the sole trigger for applying fills, but they are mandatory evidence for terminal inventory classification.

### 5.4 Target lifecycle

```text
PREPARED
→ ENTRY_SUBMITTING
→ ENTRY_ACCEPTED
→ ENTRY_MATCHED
→ ENTRY_SETTLING
→ ENTRY_CONFIRMED
→ POSITION_ACTIVE
→ EXIT_REQUESTED
→ EXIT_SUBMITTING
→ EXIT_MATCHED
→ EXIT_SETTLING
→ FLAT_CONFIRMED

Alternative terminals:
NO_FILL_CONFIRMED
FLAT_WITH_DUST
RESIDUAL_EXPOSURE
UNKNOWN_RECONCILING
MANUAL_INTERVENTION_REQUIRED
FAILED
```

`PASS_N7_ONE_SHOT_FLAT` may remain as a backward-compatible display alias only when the underlying terminal state is `FLAT_CONFIRMED`.

### 5.5 Correlation spine

Every event and artifact must retain enough identifiers to follow one lifecycle:

```text
run_id
→ window_id + market_id + token_id
→ strategy_id + intent_id
→ plan_id + request_fingerprint
→ submission_attempt_id + lineage_id
→ local_order_id + client_order_id + venue_order_id
→ trade_id / execution_id
```

An event that cannot be correlated must be staged for recovery or classified as unexpected. It must not silently mutate strategy-owned state.

---

## 6. Global safety decisions

These decisions apply to all phases.

### 6.1 Configuration authority

- Layered YAML is the canonical operator/developer configuration surface.
- The canonical live composition is strategy + risk + execution + runtime YAML resolved into one typed `ResolvedRunConfig` and one generated sealed execution artifact.
- Deleted historical JSON files must not be restored as production configuration authority.
- Legacy CLI aliases may remain only if they delegate to the same YAML resolver and N7 host. They must not maintain a second live configuration model.

### 6.2 Admission controls

Use two distinct concepts:

1. `lifecycle_safety_invariants_present` — code capability; mandatory, not operator-disableable for live.
2. `tiny_live_admitted` — release/admission state; defaults false and remains false until Phase 9 passes.

Do not use one user-editable boolean to represent both. `TYREX_N7_FORBID_LIVE=1` and CI/pytest mutation blocks remain effective in every phase.

### 6.3 Order policy for the admission experiment

- Entry and exit use marketable limit pricing.
- For the Phase 10 one-shot experiment, use FAK semantics so unfilled remainder does not rest unexpectedly.
- Exit pricing continues to use the existing fresh bid-side depth walk and absolute floor policy.
- If the installed SDK cannot represent the required FAK semantics unambiguously, stop before implementation of the submission slice and record a blocking decision. Do not silently substitute GTC.

### 6.4 Persistence policy

Execution safety state is stored under `var/runtime_state/`, not disposable reports.

Durable state must be versioned and atomically replaced. At minimum it includes:

- account baseline fingerprint and selected-market rows
- order and session obligations
- correlation/lineage mapping
- venue order IDs
- cumulative matched high-water marks
- applied trade/execution IDs
- lifecycle phase
- confirmed and sellable quantity snapshots
- pending exit request and attempts
- last reconciliation result and recovery-required flag

Reports under `var/runs/` are evidence, not the authority required to resume safely.

### 6.5 Deadline policy

All production deadlines must be explicit fields in the sealed execution configuration. No developer may introduce an unreported sleep as lifecycle control.

Required fields:

- `submission_ack_timeout_ms`
- `match_evidence_deadline_ms`
- `user_stream_reconnect_deadline_ms`
- `rest_recovery_deadline_ms`
- settlement poll/backoff/deadline settings
- `exit_ack_timeout_ms`
- `exit_retry_time_budget_ms`
- residual/manual-intervention deadline

Phase 2 defines and validates the schema. Exact production values must be frozen before Phase 9 operational acceptance.

### 6.6 Decision register

The following decisions are part of this plan. A developer must not reopen or silently change a locked decision. A blocking decision must be resolved in the named phase before its gate can pass.

| ID | Decision | Status | Resolution/gate |
|---|---|---|---|
| D-01 | Layered YAML is the production configuration authority | LOCKED | Phase 1 removes competing active defaults |
| D-02 | `LiveOMS` remains the single execution-state writer | LOCKED | Phases 3–4 route every source through it/shared applier |
| D-03 | Matched, confirmed, and sellable quantities are separate | LOCKED | Phases 3 and 6 tests enforce it |
| D-04 | Use marketable-limit FAK semantics for the one-shot admission experiment | LOCKED_WITH_SDK_CHECK | Phase 3 proves SDK mapping; otherwise STOP |
| D-05 | Keep feeds in the active compose/session and stop evaluation only | LOCKED | Phase 7; do not introduce a second live engine |
| D-06 | Latency budget misses are warnings; stale/economic revalidation failures block | LOCKED | Phase 7 reporting/revalidation |
| D-07 | Historical account positions remain visible but are not automatically strategy-owned | LOCKED | Phase 8 baseline/reconciliation |
| D-08 | Exact production deadlines | OPEN_BLOCKING_PHASE_9 | Define schema in Phase 2; freeze reviewed values before Phase 9 PASS |
| D-09 | Durable lifecycle-state schema/version | OPEN_BLOCKING_PHASE_2 | Phase 2 completion report records the final schema |
| D-10 | One human-authorized experiment only; no automatic retry/repeat | LOCKED | Phase 10 |

If a locked decision becomes technically impossible, stop the phase and write a decision amendment with evidence and safety impact before changing implementation direction.

---

# Foundation

## Phase 1 — Repository consistency

### Objective

Produce a clean, reproducible baseline so lifecycle regressions can be distinguished from configuration or documentation failures.

### Preconditions

- Work on `rest_project` from a clean worktree.
- Record starting HEAD and existing full-suite result.
- No live flags or venue mutation commands.

### Work packages

| ID | Work | Primary files | Required result |
|---|---|---|---|
| FND-01 | Inventory every active reference to deleted configuration files | `README.md`, `Docs/latest/**`, `src/**`, `tools/**`, `tests/**` | Machine-readable list in completion report |
| FND-02 | Declare YAML profiles canonical and document the composition order | `Docs/latest/how_to/configuration.md`, `Docs/latest/how_to/run_modes.md` | One documented configuration model |
| FND-03 | Migrate tests that require production-like configuration to YAML resolution | `tests/test_yaml_*`, affected R/N tests | No test depends on deleted production JSON |
| FND-04 | Move truly fixture-specific configuration into `tests/fixtures/config/` if JSON is useful for historical host tests | affected tests/fixtures | Fixture purpose explicit; not operator config |
| FND-05 | Make `n7-preflight` and `n7-live` delegate to the canonical YAML resolver or require explicit existing inputs | `application/cli.py`, `runtime/yaml_config/**` | No default path points to a missing file; no second engine |
| FND-06 | Update/remove stale observe and shadow commands | `README.md`, `Docs/latest/**`, CLI help | Every documented command resolves existing files |
| FND-07 | Validate install metadata and dependency set | `pyproject.toml` | Editable install supports CLI/tests with documented extras |
| FND-08 | Run link/config consistency checks, targeted CLI tests, then the full suite | tests/docs/CLI | Zero unexpected failures |

### Required tests

- Every documented active config path exists.
- `run --mode observe --validate-config` succeeds with canonical fixture profiles.
- `run --mode shadow --validate-config` succeeds.
- `run --mode live --validate-config` succeeds without arming mutations.
- Legacy aliases, if retained, resolve through the same typed YAML configuration.
- Unknown YAML keys and unsafe combinations fail closed.
- Full `pytest` suite passes.

### Evidence

- Baseline report with before/after failing-test counts
- Updated CLI help capture
- Canonical configuration map
- `git diff --check` and static/lint result for changed files

### Exit gate

```text
PASS when:
- full suite is green,
- no active documentation/CLI path references a deleted config,
- YAML is the single production configuration authority,
- real venue mutations attempted = 0.
```

Do not begin lifecycle contract changes until this gate passes.

---

# Terminal safety

## Phase 2 — Execution obligations

### Objective

Make false flatness and false PASS impossible immediately after any real submission attempt, even before complete fill wiring exists.

### Design

Use two related obligation types:

#### OrderExecutionObligation

One obligation per entry or exit submission.

Required fields:

```text
obligation_id
run_id, lineage_id, intent_id, plan_id
submission_attempt_id
side, market_id, token_id, requested_qty
local_order_id, client_order_id, venue_order_id?
state
created_at, updated_at, deadline_at
resolution_reason?
```

States:

```text
PREPARED
SUBMITTING
VENUE_ACCEPTED
RESOLVING
RESOLVED_NO_FILL
RESOLVED_FILLED
RESOLVED_CANCELED
UNKNOWN
FAILED
```

The obligation is persisted as `SUBMITTING` immediately before the network mutation. A timeout after dispatch becomes `UNKNOWN`, not `FAILED` and not flat.

#### SessionExposureObligation

One obligation for the complete one-shot session. It opens when the first network submission may have changed venue state and closes only after `NO_FILL_CONFIRMED` or authoritative return to baseline.

### Work packages

| ID | Work | Primary files/symbols | Tests/evidence |
|---|---|---|---|
| TSO-01 | Add obligation contracts and registry | new `execution/polymarket/execution_obligation.py` | state-transition unit tests |
| TSO-02 | Add atomic persistence and schema version | `persistence/`, `runtime_state` paths | crash/reload and incompatible-version tests |
| TSO-03 | Open order obligation before transport dispatch | `LiveOMS.submit`, reporting pre-mutation barrier | transport exception/timeout tests |
| TSO-04 | Open session obligation on first possible venue mutation | `N7OneShotHost` | one per session/lineage test |
| TSO-05 | Resolve rejected-before-mutation distinctly from uncertain-after-dispatch | `LiveOMS` result handling | rejected vs timeout matrix |
| TSO-06 | Add conservative terminal blocker | new `runtime/n7_terminal.py`, `n7_live_session.py` | open/unknown obligation can never PASS |
| TSO-07 | Add obligation state to reporting summaries without making reports authoritative | reporting adapters/contracts | contradiction event tests |
| TSO-08 | Add immutable live safety/admission gates | N7 arm path + config resolution | arm refused when capability/admission absent |
| TSO-09 | Add required deadline fields to typed configuration, with live admission still false | YAML schema / sealed config | missing/invalid deadline fails config |

### Terminal rules introduced in this phase

- ACK is not a fill.
- `SUBMITTING`, `VENUE_ACCEPTED`, `RESOLVING`, or `UNKNOWN` obligations block PASS.
- Empty local portfolio after submission is not terminal evidence.
- Reconciliation unavailable or unresolved is non-PASS.
- Reports must emit a critical contradiction if any old path attempts to claim flat while an obligation is open.

### Required tests

- Crash/exception before transport call: no venue mutation; obligation safely failed/canceled.
- Timeout after transport call: obligation `UNKNOWN`; session non-PASS.
- Accepted order with zero local fills: obligation open; session non-PASS.
- Empty portfolio plus open obligation: never flat.
- Exit submission creates its own obligation.
- Restart reloads obligations and blocks new entry.
- OBSERVE and SHADOW behavior remains unchanged.

### Exit gate

```text
PASS when no execution path can produce PASS after a possible venue mutation
unless all order/session obligations are resolved by positive evidence.
Real venue mutations remain disabled.
```

---

## Phase 3 — Complete submission evidence

### Objective

Preserve and normalize enough submission evidence for the OMS to detect immediate matches, correlate later events, and apply execution information exactly once.

### Contract changes

Extend `SubmitOrderResult` with typed fields equivalent to:

```text
ok
venue_order_id
client_order_id?
status
trade_ids: tuple[str, ...]
making_amount?
taking_amount?
cumulative_matched_qty?
remaining_qty?
uncertain
error?
raw_redacted
```

The adapter owns SDK interpretation. No runtime host may inspect SDK-native response objects.

### Work packages

| ID | Work | Primary files/symbols | Tests/evidence |
|---|---|---|---|
| EVD-01 | Capture sanitized real SDK response shapes as fixtures without secrets | test fixtures / implementation evidence | accepted, matched, delayed, rejected examples |
| EVD-02 | Resolve BUY/SELL making/taking amount semantics from SDK fixtures/documentation | adapter contract note | table-driven unit tests |
| EVD-03 | Extend `SubmitOrderResult` atomically across every producer | `transport.py`, fake/spy transports, constructors | contract suite |
| EVD-04 | Preserve fields in `_submit_result_from_sdk` | `mutation_transport.py` | mapper tests |
| EVD-05 | Add normalized response-match event creation | `normalize.py`, `LiveOMS` | immediate-match test |
| EVD-06 | Add cumulative matched high-water mark to `OrderStore` | `order_store.py` | repeated/decreasing/out-of-order tests |
| EVD-07 | Add matched-exposure projection bound to lineage | new `portfolio/matched_exposure.py` or explicit projection module | orphan and dedupe tests |
| EVD-08 | Keep MATCHED separate from CONFIRMED inventory | OMS/fill application boundary | test that MATCHED wakes exposure but does not create sellable inventory |
| EVD-09 | Record complete redacted submit evidence in critical audit lane | reporting adapter | audit schema test |

### HTTP/WebSocket idempotency contract

| Arrival order | Required behavior |
|---|---|
| HTTP response first | Apply identifiers/status/match HWM; later stream confirms without duplication |
| WebSocket first with known client ID | Stage/correlate, then bind to local order when HTTP returns |
| WebSocket first with venue ID only | Stage by venue ID; never mutate unrelated strategy state |
| Duplicate trade ID | Apply once |
| Repeated cumulative `size_matched` | Apply only positive delta above HWM |
| Lower/out-of-order cumulative value | Ignore for quantity; emit diagnostic |

### Required tests

- Immediate full match response
- Immediate partial match response
- Accepted/resting response
- Delayed/uncertain response
- Rejected response
- Duplicate trade IDs
- Repeated and decreasing cumulative matched values
- BUY and SELL amount mapping
- Redaction/no-secret assertion
- MATCHED exposure exists while confirmed and sellable quantities remain zero

### Exit gate

```text
PASS when complete submission evidence reaches LiveOMS without SDK leakage,
one match produces one matched-exposure delta, and MATCHED is not treated as
confirmed/sellable inventory.
```

---

# Live event wiring

## Phase 4 — Continuous authenticated user stream

### Objective

Run an authenticated account-wide Polymarket stream from before mutation arm until terminal classification, feeding the same single-writer execution path used by submission responses.

### Work packages

| ID | Work | Primary files/symbols | Tests/evidence |
|---|---|---|---|
| STR-01 | Add a live supervisor separate from the timed read-only probe | new `execution/polymarket/user_stream_live.py` | lifecycle unit tests |
| STR-02 | Subscribe account-wide and normalize order/trade messages | `sdk_secure.py`, `normalize.py` | sanitized message fixtures |
| STR-03 | Start stream and prove readiness before mutation arm | `N7OneShotHost`, `n7_live_session.py` | ordering assertion |
| STR-04 | Route normalized messages to LiveOMS/host ingestion | `LiveOMS`, `N6LiveHost.ingest_confirmed_trade` or extracted shared applier | end-to-end fake stream |
| STR-05 | Stage early/unbound events and correlate later | stream supervisor / OMS correlation registry | WS-before-HTTP tests |
| STR-06 | Implement reconnect with bounded backoff and gap marker | stream supervisor | disconnect tests |
| STR-07 | On disconnect, mark readiness false and block new exposure | execution readiness | gate tests |
| STR-08 | Trigger read-only recovery after a stream gap | hook for Phase 8 reconciliation | recovery-request event |
| STR-09 | Stop stream only after terminal classification or explicit operator handoff | N7 termination | teardown ordering test |

### Single-writer rule

HTTP response, user stream, and later REST repair must not update Portfolio independently. They all normalize evidence and pass it through the same idempotent order/execution application boundary.

### Required tests

- Stream is healthy before `arm_operator_live` can succeed.
- Immediate stream event during HTTP submit is retained.
- HTTP-first and stream-first yield identical stores and exposure.
- Disconnect blocks new entry.
- Reconnect backfills the gap before readiness returns.
- Duplicate messages do not duplicate fills/exposure.
- Account-wide unexpected events remain visible but do not become strategy-owned without correlation.
- Termination does not close the stream before final reconciliation.

### Exit gate

```text
PASS when a continuously running authenticated stream feeds the single writer,
is active before mutation arm, and any gap forces recovery/non-readiness.
All tests use fakes; real venue mutations = 0.
```

---

## Phase 5 — Exposure-driven exit supervisor

### Objective

Replace fixed sleeps and local-portfolio polling with an event-driven supervisor that reacts immediately to matched strategy-owned exposure and keeps exit work pending until terminal resolution.

### Component contract

Add `runtime/exit_supervisor.py` with inputs such as:

```text
MatchedExposureChanged
SettlementChanged
InventorySellable
BookViewChanged
ExitIntent / FlattenIntent
TimerDeadline
KillSwitchActivated
ReconciliationResult
```

The supervisor owns orchestration, not pricing, fills, portfolio truth, or strategy economics.

### Work packages

| ID | Work | Primary files/symbols | Tests/evidence |
|---|---|---|---|
| SUP-01 | Add supervisor state and event interface | new `runtime/exit_supervisor.py` | state-machine unit tests |
| SUP-02 | Wake on first positive matched-exposure delta | matched exposure → supervisor | delayed-match test |
| SUP-03 | Record exit need even when inventory is not sellable | supervisor pending state | MATCHED/no-balance test |
| SUP-04 | Route strategy exit, kill, thesis, and time-exit requests through one supervisor | strategy/host intent routing | precedence tests |
| SUP-05 | Replace `sleep(3)+is_flat` control in N7 | `n7_live_session.py`, `n7_oneshot_host.py` | no fixed-sleep lifecycle test |
| SUP-06 | Track partial entry and unfilled remainder separately | supervisor + order state | partial-fill matrix |
| SUP-07 | Track exit requested, submitted, matched, remaining, and retry budget | supervisor state | partial-exit/retry tests |
| SUP-08 | Escalate deadlines to recovery/manual intervention, never false success | supervisor + terminal blocker | timeout tests |
| SUP-09 | Persist supervisor state required for restart | runtime state | crash/reload test |

### Required behavior

- Matched exposure activates supervision immediately.
- An exit request survives a temporary non-sellable interval.
- Only owned matched/confirmed quantity participates.
- Partial entry supervises only actual matched exposure.
- Entry remainder is canceled or explicitly tracked according to FAK terminal evidence.
- Exit retry is bounded by attempts and time budget.
- Kill switch accelerates safe reduction but never guesses quantity.

### Exit gate

```text
PASS when every matched strategy-owned exposure wakes the supervisor,
no fixed sleep controls exit reachability, and unresolved exposure always
remains pending or non-PASS.
```

---

## Phase 6 — Shared settlement and sellability

### Objective

Reuse the proven R7 settlement model so N7 supervises at MATCHED but submits SELL only for confirmed, sellable, strategy-owned quantity.

### Reuse targets

- `execution/polymarket/settlement.py`
- `TradeSettlementStatus`
- `wait_for_entry_settlement`
- `evaluate_sell_readiness`
- `classify_flatness`
- `execution/polymarket/mutation_lifecycle.py`
- existing R7 tests and incident-derived patterns

N7 must reuse shared contracts, not import R7 one-shot orchestration wholesale.

### Work packages

| ID | Work | Primary files/symbols | Tests/evidence |
|---|---|---|---|
| STL-01 | Introduce/confirm separate matched, confirmed, sellable projections | settlement/exposure/portfolio boundaries | three-axis unit tests |
| STL-02 | Feed stream/REST settlement statuses into the shared settlement evaluator | stream normalize + settlement | MATCHED→MINED→CONFIRMED tests |
| STL-03 | Drive N7 `MutationPhase` transitions | N7 host/session | transition matrix |
| STL-04 | Notify supervisor when inventory becomes sellable | settlement → supervisor | pending→actionable test |
| STL-05 | Compute safe SELL quantity from confirmed, balance, and remaining owned qty | sellability helper | quantity-bound tests |
| STL-06 | Route safe quantity through existing exit planner and LiveOMS | `N7OneShotHost.run_bounded_exit_ladder` | fake full lifecycle |
| STL-07 | Handle settlement retry/failure without clearing exposure | terminal/recovery path | retry/failure tests |
| STL-08 | Resolve entry/exit obligations only from settlement/order terminal evidence | obligations registry | premature-resolution tests |
| STL-09 | Apply confirmed fills idempotently to FillLedger/Portfolio | shared execution applier | duplicate/restart tests |

### Mandatory quantity formula

```text
sell_qty = min(
    confirmed_strategy_owned_qty,
    funder_conditional_balance,
    remaining_unexited_qty
)
```

Planned quantity and insert acknowledgement never participate in this minimum.

### Required tests

- MATCHED but not CONFIRMED: supervisor active, SELL blocked.
- MINED but not CONFIRMED: SELL blocked.
- CONFIRMED but balance zero/lagging: exit remains pending.
- Confirmed quantity greater than balance: SELL capped to balance.
- Partial confirmed inventory: only confirmed portion is actionable.
- Settlement failure: non-PASS/manual intervention.
- Duplicate settlement/trade evidence: exactly-once portfolio application.
- Partial exit and dust classification.

### Exit gate

```text
PASS when supervision begins at MATCHED, confirmed accounting changes only
from confirmed evidence, and SELL cannot exceed authenticated sellable inventory.
```

---

# Freshness and authority

## Phase 7 — Continuous feeds and pre-submit revalidation

### Objective

Keep market/reference/user feeds alive from candidate creation through terminal state and refuse stale or materially changed entry plans immediately before submission.

### Target orchestration

Preferred design:

```text
compose owns active feeds and stores
→ strategy emits one candidate
→ stop further entry evaluation, not the feeds
→ risk + tentative plan
→ immediate revalidation from current MarketStateStore/BookView
→ submit within the same live session
→ feeds continue for settlement, exit, and reconciliation
```

Do not build a second live engine or a second quote path.

### Work packages

| ID | Work | Primary files/symbols | Tests/evidence |
|---|---|---|---|
| FRH-01 | Refactor `stop_requested` so it stops entry evaluation only | `live_zgap_compose.py`, `n7_live_session.py` | feeds-alive assertion |
| FRH-02 | Submit from inside the active composed session | compose/session/host integration | no teardown-before-submit test |
| FRH-03 | Reuse `MarketStateStore → BookView` as the only live quote source | market data and plan wiring | architecture/import test |
| FRH-04 | Add immediate pre-submit book/window/binding revalidation | `planning/book_revalidation.py`, host | stale/version-change tests |
| FRH-05 | Re-evaluate strategy/risk hard gates without changing thresholds | strategy binding/risk context | edge/spread/cutoff tests |
| FRH-06 | Recheck fee-inclusive budget and order minimum | planner/live sizing | cap tests |
| FRH-07 | Record immutable book fingerprint, versions, candidate age, and revalidation result | reporting | audit schema test |
| FRH-08 | Add monotonic latency timestamps | market→eval→intent→risk→plan→revalidate→HTTP→match→exit wake | summary/ordering tests |
| FRH-09 | Keep exit bid-side books fresh through supervisor retries | exit planner/book feed | moving-book tests |

### Hard revalidation failures

Submission must be refused before mutation when any of these changes:

- selected market/window/token binding
- entry cutoff or window phase
- book synchronization/readiness
- book freshness
- required side/depth
- spread/risk gate
- economic edge below the existing threshold
- fee-inclusive debit above `$5.00`
- reporting critical-audit health
- user-stream readiness
- account/recovery readiness
- kill switch or open prior session obligation

Latency budget misses are warnings unless a freshness/revalidation rule fails. Do not invent a performance failure that bypasses economic revalidation.

### Exit gate

```text
PASS when feeds remain active at submit and through exit, every submission has
a fresh revalidation artifact, and a stale candidate cannot reach transport.
```

---

## Phase 8 — Baseline-aware terminal reconciliation

### Objective

Make read-only venue evidence the authority for final session classification, recovery, restart, and proof of no-fill/flatness relative to the pre-run account baseline.

### Baseline artifact

Capture and persist before mutation arm:

```text
account/funder/proxy identity fingerprint
selected market, condition, token IDs, window
selected-token conditional balances
owned and unexpected open orders
recent relevant trades / pagination cursor or bounded time range
acknowledged historical positions
capture timestamp and source health
```

The baseline is immutable for a session and stored in durable runtime state plus a report attachment.

### Work packages

| ID | Work | Primary files/symbols | Tests/evidence |
|---|---|---|---|
| AUT-01 | Attach read-only transport to the N7 host beside mutation transport | `sdk_readonly.py`, N7/N6 host | capability test |
| AUT-02 | Capture and seal selected-market baseline during preflight | `n7_preflight.py`, `live_preflight.py` | baseline fixture |
| AUT-03 | Add watchdog for accepted/unknown obligations with no match evidence | runtime watchdog | timeout→REST test |
| AUT-04 | Reconcile open orders, trades, positions/balances, local stores, and obligations | `ReconciliationService` | matrix tests |
| AUT-05 | Auto-repair `FILL_MISSING_LOCAL` only for correlated owned trades | reconciliation + shared fill applier | owned/unowned repair tests |
| AUT-06 | Block and expose unexpected/unowned venue evidence | reconciliation/readiness | unexpected exposure tests |
| AUT-07 | Rebuild after disconnect/restart before allowing new entry | runtime state + REST reconstruction | restart fixtures |
| AUT-08 | Implement final pure terminal classifier | `runtime/n7_terminal.py` | exhaustive outcome matrix |
| AUT-09 | Make reporting reject/flag contradictions | reporting summary | contradiction tests |
| AUT-10 | Persist final reconciliation and obligation resolution atomically | runtime state | crash-at-terminal test |

### Exact `NO_FILL_CONFIRMED` proof

All conditions are required:

1. Entry order is definitively rejected before mutation, canceled, expired, or otherwise venue-terminal.
2. Venue cumulative matched quantity is zero.
3. No correlated trade exists in the bounded/full required trade query.
4. Selected-token balance equals baseline.
5. No owned open order remains.
6. All order/session obligations are resolved.
7. Reconciliation completed without disagreement.

Timeout, missing data, or empty local portfolio is never `NO_FILL_CONFIRMED`.

### Exact `FLAT_CONFIRMED` proof

All conditions are required:

1. Every owned entry and exit submission has a resolved obligation.
2. No owned open order remains.
3. All correlated trades were applied exactly once locally.
4. Selected-token balance returned to baseline, subject only to the explicit dust threshold.
5. Local order/fill/portfolio/lifecycle state agrees with venue evidence.
6. Reconciliation has no unresolved or manual-intervention classification.

### Terminal result precedence

```text
unexpected/unowned or irreconcilable evidence
  → MANUAL_INTERVENTION_REQUIRED

missing/temporarily unavailable evidence or unknown obligation
  → UNKNOWN_RECONCILING

known remaining balance/open owned order
  → RESIDUAL_EXPOSURE or EXIT_PENDING

all no-fill conditions true
  → NO_FILL_CONFIRMED

all return-to-baseline conditions true
  → FLAT_CONFIRMED or FLAT_WITH_DUST

otherwise
  → FAILED (only for definitive non-exposure operational failure)
```

### Exit gate

```text
PASS when disconnect, timeout, restart, missing local fill, and final-state
classification all produce either consistent reconstruction or explicit non-PASS;
success requires positive baseline-aware venue evidence.
```

---

# Acceptance

## Phase 9 — Non-live validation ladder

### Objective

Prove the full lifecycle without real venue mutation and produce the signed evidence required to admit exactly one Phase 10 experiment.

### Level 1 — Unit and contract tests

Mandatory scenarios:

| # | Scenario | Primary phase |
|---:|---|---:|
| 1 | Submit rejected before mutation | 2–3 |
| 2 | Submit timeout; venue order absent | 2, 8 |
| 3 | Submit timeout; venue order exists | 2, 8 |
| 4 | HTTP immediate full match | 3 |
| 5 | HTTP immediate partial match | 3 |
| 6 | HTTP before WebSocket | 3–4 |
| 7 | WebSocket before HTTP | 3–4 |
| 8 | Duplicate trade event | 3–4 |
| 9 | Repeated/decreasing matched HWM | 3–4 |
| 10 | Match after the historical three-second boundary | 5 |
| 11 | Partial entry with remainder terminal/canceled | 5–6 |
| 12 | MATCHED not yet sellable | 5–6 |
| 13 | MATCHED→MINED→CONFIRMED | 6 |
| 14 | Settlement retry | 6 |
| 15 | Settlement failure | 6, 8 |
| 16 | Confirmed inventory with lagging balance | 6 |
| 17 | Partial exit | 5–6 |
| 18 | Duplicate exit execution | 3–6 |
| 19 | Book changes before submission | 7 |
| 20 | Window changes before submission | 7 |
| 21 | User-stream disconnect during fill | 4, 8 |
| 22 | Reconnect misses trade and REST repairs | 4, 8 |
| 23 | Restart with open order/obligation | 2, 8 |
| 24 | Venue inventory without local fill | 8 |
| 25 | Unexpected account exposure | 8 |
| 26 | Confirmed no-fill | 8 |
| 27 | Authoritative flat | 8 |
| 28 | False-flat incident replay | 2, 8 |
| 29 | Reporting contradiction | 2, 8 |
| 30 | No real mutation in tests | all |

Planned test modules:

```text
tests/test_n7_terminal_safety.py
tests/test_exec_lifecycle_obligations.py
tests/test_exec_lifecycle_response_match.py
tests/test_exec_lifecycle_user_stream.py
tests/test_exec_lifecycle_ordering.py
tests/test_exec_lifecycle_exit_wake.py
tests/test_exec_lifecycle_settlement.py
tests/test_exec_lifecycle_recovery.py
tests/test_n7_continuous_submit.py
tests/test_n7_lifecycle_acceptance.py
```

Existing tests may be extended where ownership already matches, but the completion report must map every scenario above to an exact test name.

### Level 2 — Integrated FakeTransport lifecycle

Exercise the production N7 orchestration with fake venue and stream adapters:

```text
strategy intent
→ risk
→ plan
→ revalidation
→ submit
→ HTTP/stream execution evidence
→ matched exposure
→ settlement
→ sellability
→ exit supervisor
→ exit
→ baseline reconciliation
→ FLAT_CONFIRMED
```

Tests must not bypass production wiring by directly inserting a fill into Portfolio.

### Level 3 — Incident replay

Sanitize the evidence from `yaml_live_20260731T143127Z` into a deterministic fixture.

Prove:

- the old lifecycle could produce a false flat;
- the new Phase 2 blocker returns non-PASS;
- response/stream evidence creates matched exposure exactly once;
- the supervisor requests exit;
- unresolved reconciliation cannot produce PASS.

Preserve the historical run directory unchanged.

### Level 4 — OBSERVE and SHADOW regression

- Run canonical fixture OBSERVE.
- Run canonical fixture SHADOW entry→exit.
- Run resolution-aware SHADOW scenarios.
- Confirm Z-Gap decisions and thresholds are unchanged.
- Confirm no live credentials or mutation transports are required.

### Level 5 — Operational market-data acceptance

On a venue-reachable operator host, run mutation-disabled OBSERVE/SHADOW across at least two consecutive BTC five-minute rollovers.

Required evidence:

- active and prepared window bindings are correct;
- promotion preserves correct market/token identity;
- PTB K is sealed against the canonical boundary;
- books become READY through REST/WS rules;
- fresh real bids/asks reach Z-Gap evaluation;
- cold-gap threshold is respected or explicitly blocks readiness;
- reconnect/recovery behavior is recorded;
- venue mutations attempted = 0.

### Level 6 — Authenticated read-only preflight

Required checks:

- identities and signer/funder roles match sealed configuration;
- public server time and feeds are healthy;
- authenticated user stream connects;
- open orders are fully paginated and classified;
- selected-market baseline is captured;
- historical positions are acknowledged but not silently owned by N7;
- unexpected exposure blocks admission;
- mutation transport remains unarmed;
- `real_venue_mutations = 0`.

### Required Phase 9 evidence package

```text
phase_9_acceptance_report.md
full_test_result.txt or captured command/result
incident_replay_result.json
fake_lifecycle_run/
observe_shadow_regression/
two_rollover_market_data/
authenticated_readonly_preflight/
sealed_config_fingerprint.txt
tiny_live_admission_checklist.md
```

### Phase 9 admission checklist

Every item must be YES:

- false PASS is impossible;
- complete submission evidence is retained;
- authenticated stream starts before arm;
- HTTP/stream ordering is idempotent;
- matched/confirmed/sellable quantities remain distinct;
- every matched exposure activates supervision;
- settlement and sellability reuse R7 rules;
- exit requests survive non-sellable intervals;
- read-only recovery and restart reconstruction pass;
- feeds remain alive through dispatch;
- pre-submit revalidation blocks stale candidates;
- no-fill and flatness require baseline-aware venue proof;
- reporting is contradiction-free;
- full repository suite is green;
- two-rollover operational acceptance passes;
- authenticated preflight is healthy;
- sealed timeouts and FAK order policy are reviewed;
- one account, one market, one lineage, fee-inclusive debit ≤ `$5.00`;
- live admission is still disabled pending operator review.

### Exit gate

```text
PASS only when all six non-live levels and every admission item pass.
Phase 9 PASS may set the reviewed release artifact that allows Phase 10,
but it must not itself submit an order.
```

---

## Phase 10 — One monitored tiny-live experiment

### Objective

Perform exactly one operator-authorized, fee-inclusive `$5.00` maximum, single-window N7 lifecycle to validate the repaired execution wiring. This is an acceptance experiment, not routine trading.

### Authorization boundary

- Only the human operator may invoke the real mutation flag.
- An agent/developer performing Phases 1–9 must not run this phase automatically.
- Phase 9 PASS evidence must be reviewed before authorization.
- The worktree and sealed configuration fingerprint must match the reviewed candidate.
- `TYREX_N7_FORBID_LIVE` is unset only intentionally on the operator host for this experiment.

### Pre-run checklist

1. Clean committed HEAD matches the accepted Phase 9 report.
2. Full suite and required targeted suites are green at that HEAD.
3. Sealed config fingerprint matches the reviewed artifact.
4. `tiny_live_admitted` is enabled only by the reviewed release/admission mechanism.
5. Credentials, signer, funder, proxy, and network identity pass.
6. User stream is connected and healthy.
7. Public/reference/market feeds are healthy.
8. Selected-market baseline is sealed.
9. Open orders are zero or explicitly expected/owned.
10. Unexpected selected-market exposure is zero.
11. All previous obligations are resolved.
12. Reporting critical audit lane is healthy.
13. Fee-inclusive entry cap is exactly `$5.00` or lower.
14. FAK behavior, exit floor, deadlines, and manual handoff procedure are visible to the operator.

Any failed item aborts before mutation.

### Expected success sequence

```text
sealed PTB and live Z-Gap decision
→ EnterIntent
→ risk approval
→ fresh pre-submit revalidation
→ persisted submission obligation
→ venue submission
→ HTTP/user-stream match awareness
→ matched exposure supervision
→ confirmed settlement
→ sellable inventory
→ exit request and fresh exit plan
→ exit submission/fill
→ authenticated reconciliation
→ return to baseline
→ FLAT_CONFIRMED or approved FLAT_WITH_DUST
→ mutations disabled
```

### Live monitoring requirements

The operator must be able to observe:

- user-stream health;
- current lifecycle phase;
- entry and exit obligation states;
- matched, confirmed, sellable, and remaining quantities;
- open owned orders;
- exit supervisor state/deadline;
- reconciliation state;
- reporting health;
- real venue mutation count.

### Stop/handoff conditions

Immediately disable new mutations and enter recovery/manual handoff for:

- user-stream gap that cannot be recovered within deadline;
- unknown submission;
- unexpected order/trade/position;
- settlement failure;
- exit deadline exceeded;
- reconciliation disagreement;
- reporting critical-audit failure after mutation;
- operator interrupt;
- residual exposure at the manual-intervention deadline.

Do not guess inventory or submit an unbounded emergency order. The handoff report must contain the known owned quantity, open order IDs, venue evidence, and next safe operator action.

### Required post-run evidence

- manifest and sealed configuration fingerprint
- complete critical audit stream
- candidate and pre-submit revalidation evidence
- HTTP submission results and user-stream correlations
- obligation state history
- matched/confirmed/sellable quantity history
- lifecycle and supervisor transitions
- entry/exit order and trade IDs
- final open-order/trade/balance reconciliation
- baseline delta
- terminal classifier inputs and result
- mutation count
- operator incident/handoff record if not flat

### Phase 10 success criteria

Success requires all of:

- one permitted entry lineage only;
- fee-inclusive BUY debit ≤ `$5.00`;
- every execution applied exactly once;
- no unowned execution attributed to the strategy;
- no owned open order remains;
- selected-market balance returned to baseline or approved dust;
- obligations resolved;
- reconciliation clean;
- terminal state `FLAT_CONFIRMED` or policy-valid `FLAT_WITH_DUST`;
- reports contain no contradictions;
- mutations are forced off at termination.

`NO_FILL_CONFIRMED` is a safe experiment outcome but does not validate the full entry→exit path. It may justify a separately reviewed repeat only after the evidence is examined. One successful run does not authorize continuous operation or a higher capital limit.

### Exit gate

```text
PASS when the operator-reviewed evidence proves the complete lifecycle and
authoritative return to baseline. Stop after this one experiment for review.
No automatic second live run is authorized by Phase 10 PASS.
```

---

## 7. Cross-phase dependency graph

```text
Phase 1 repository consistency
  → Phase 2 obligations and conservative terminal block
    → Phase 3 complete response evidence and matched exposure
      → Phase 4 authenticated live stream
        → Phase 5 exposure-driven supervisor
          → Phase 6 settlement/sellability and safe exit
            → Phase 7 continuous feeds/revalidation
              → Phase 8 recovery and authoritative terminal proof
                → Phase 9 non-live acceptance
                  → Phase 10 one operator experiment
```

Hard dependency rules:

- Phase 2 must precede any new event wiring so incomplete later phases fail safely.
- Phase 3 must not convert MATCHED directly into confirmed/sellable inventory.
- Phase 4 must feed the same single-writer path as Phase 3.
- Phase 5 cannot submit SELL merely because matched exposure exists.
- Phase 6 owns the sellability permission.
- Phase 7 cannot introduce a second strategy, quote, risk, or OMS path.
- Phase 8 is the only authority for final PASS classification.
- Phase 9 is the only gate that may admit Phase 10.

---

## 8. File and ownership map

| Area/file | Planned responsibility | Phase |
|---|---|---:|
| `application/cli.py` | canonical YAML delegation; no missing defaults | 1 |
| `runtime/yaml_config/**` | single typed configuration authority | 1–2 |
| `execution/polymarket/execution_obligation.py` | per-order/session obligations | 2 |
| `persistence/**`, `runtime/r7_paths.py` or generic successor | atomic lifecycle runtime state | 2, 5, 8 |
| `runtime/n7_terminal.py` | pure conservative/final terminal classifier | 2, 8 |
| `execution/polymarket/transport.py` | complete `SubmitOrderResult` | 3 |
| `execution/polymarket/mutation_transport.py` | SDK response mapping | 3 |
| `execution/polymarket/normalize.py` | response/stream/REST evidence normalization | 3–4, 8 |
| `execution/polymarket/live_oms.py` | single-writer order/execution application | 2–4, 6 |
| `execution/order_store.py` | cumulative matched HWM and order status | 3–4 |
| `execution/fill_ledger.py` | exactly-once confirmed execution ledger | 3–6, 8 |
| `portfolio/matched_exposure.py` | strategy-owned matched exposure projection | 3–6 |
| `portfolio/portfolio.py` | confirmed internal accounting; not terminal oracle | 3–8 |
| `execution/polymarket/user_stream_live.py` | continuous authenticated session stream | 4 |
| `runtime/exit_supervisor.py` | exposure-driven exit orchestration | 5–7 |
| `execution/polymarket/settlement.py` | settlement and sellability rules | 6 |
| `execution/polymarket/mutation_lifecycle.py` | lifecycle phase vocabulary | 6, 8 |
| `runtime/live_zgap_compose.py` | keep feeds active through execution | 7 |
| `planning/book_revalidation.py` | immediate pre-submit validation | 7 |
| `execution/polymarket/sdk_readonly.py` | REST recovery/reconciliation reads | 8 |
| `execution/polymarket/reconciliation.py` | baseline comparison and safe repair | 8 |
| `runtime/n7_preflight.py`, `runtime/live_preflight.py` | baseline capture/readiness | 8–9 |
| `reporting/**` | audit/evidence/contradiction visibility, never runtime authority | all |
| `fake_transport.py`, tests | zero-mutation deterministic proof | 2–9 |

If implementation shows that one of these owners is incorrect, document the deviation before moving responsibility. Do not create a duplicate authority silently.

---

## 9. Recommended commit sequence

Each numbered item is intended to be reviewable and keep the suite green:

1. `chore(config): establish canonical YAML baseline and repair stale paths`
2. `feat(exec): add durable order and session execution obligations`
3. `fix(n7): block false PASS while obligations or recon are unresolved`
4. `feat(exec): preserve complete SubmitOrderResult evidence`
5. `feat(oms): apply response match evidence to matched-exposure HWM`
6. `feat(exec): add continuous authenticated UserStreamLive`
7. `feat(n7): start stream before mutation arm and recover gaps`
8. `feat(n7): add exposure-driven ExitSupervisor`
9. `feat(n7): reuse settlement and sellability for safe exit quantity`
10. `feat(n7): keep feeds alive and revalidate immediately before submit`
11. `feat(recon): add baseline-aware recovery, restart, and terminal proof`
12. `test(exec): add full lifecycle matrix and incident replay`
13. `test(ops): complete observe/shadow/two-rollover/read-only acceptance`
14. `docs(n7): publish Phase 9 admission evidence and reviewed fingerprint`
15. `ops(n7): record one operator-authorized tiny-live experiment`

Commit 15 is performed only by or under direct control of the operator after commits 1–14 are reviewed. It is an evidence/operations step, not an automatic developer action.

---

## 10. Required invariants checklist

Every invariant must have an automated test before Phase 9 passes:

1. ACK is never a fill.
2. A mutation attempt creates a durable unresolved obligation before transport dispatch.
3. A timeout after dispatch is UNKNOWN, not rejected and not flat.
4. Every venue execution delta is applied exactly once.
5. Cumulative matched quantity never moves backward or double-counts.
6. HTTP/stream event order cannot change final state.
7. Strategy ownership comes only from immutable lineage/correlation.
8. Matched, confirmed, and sellable quantities remain distinct.
9. Matched exposure activates protection immediately.
10. SELL never exceeds confirmed, sellable, remaining owned quantity.
11. Partial fills expose only executed quantity.
12. Unfilled remainder is definitively terminal or explicitly tracked.
13. Exit requests survive non-sellable settlement gaps.
14. Fixed sleeps are not lifecycle controllers.
15. Market and user feeds remain active through submission and exit.
16. Every submission uses a fresh pre-submit revalidation.
17. Stream gaps and restart block entry until recovery completes.
18. Venue inventory cannot silently become strategy-owned without correlated evidence.
19. Safe auto-repair applies only correlated owned trades.
20. Local flatness after mutation is not authoritative.
21. Reconciliation failure or uncertainty never returns PASS.
22. `NO_FILL_CONFIRMED` requires positive venue evidence.
23. Successful flatness requires return to the sealed account baseline.
24. Reports expose contradictions and cannot override runtime safety state.
25. OBSERVE, SHADOW, and LIVE retain shared contracts; only LIVE mutates the venue.
26. No Phase 1–9 test or operational acceptance performs a real venue mutation.
27. Phase 10 is one market, one lineage, ≤ `$5.00`, with no automatic repeat.

---

## 11. Final implementation instruction

Implement the plan in the stated order and stop for review at every phase gate.

The first delivery should contain only:

```text
Phase 1 — repository consistency
Phase 2 — execution obligations and conservative false-PASS prevention
Phase 3 — complete submission evidence and matched-exposure separation
```

After those phases pass, review their contracts and evidence before starting the authenticated user-stream work. Do not combine all ten phases into one large change, and do not use partial completion as authorization for a live experiment.
