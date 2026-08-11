# LIVE Lifecycle Minimal Solution — Implementation Report

## 1. Overall outcome

**COMPLETE**

Production real-LIVE execution lifecycle now progresses through entry → match → sellable → exit → exit match/confirm → authoritative terminal reconciliation. Exit submission is no longer a successful terminal stop. Ambiguous mutations reconcile before retry. Mutation reporting counts entry and exit. Focused production-composition tests and the full deterministic unit suite pass. No real venue mutations were performed.

## 2. Repository state

| Item | Value |
|---|---|
| Branch | `rest_project` (tracks `origin/rest_project`) |
| Starting HEAD | `28c4d804f0c2927579c35743bfcedb807330d768` |
| Existing changes preserved | **Yes** — no reset/clean/revert; prior uncommitted work retained |
| SDK pin | `polymarket-client==0.2.0` (unchanged) |

## 3. Architecture implemented

Run-to-terminal orchestration remains owned by the existing N7 LIVE path:

```text
n7_live_session._run_in_session_mutation_phase
  → N7OneShotHost.try_enter / run_bounded_exit_ladder
  → run_exposure_exit_supervision (continuous evidence loop)
  → LiveOMS + SdkMutationTransport / SdkReadonlyTransport
  → ExitSupervisor + SharedSettlementCoordinator
  → BaselineAwareReconciliation + classify_n7_terminal  (sole PASS authority)
```

**No second lifecycle engine was created.** Changes complete and connect existing components (LiveOMS, ExitSupervisor, SharedSettlementCoordinator, N7OneShotHost, SdkMutationTransport, SdkReadonlyTransport, LifecycleRuntimeStateStore).

OBSERVE/SHADOW were not extended as validation stages. Shared components keep existing public contracts where practical.

## 4. Changes by requirement A–K

### A. Side-aware, role-aware lifecycle evidence

| | |
|---|---|
| Files | `execution_role.py` (new), `transport.py`, `live_oms.py`, `exit_supervisor.py` |
| Before | Matched-exposure callbacks lacked side/role; `ExecutionRole` did not exist |
| After | `ExecutionRole.ENTRY_BUY` / `EXIT_SELL`; match/settlement payloads include side, role, token, market, intent/plan, lineage, cumulative/delta matched, trade IDs, settlement status |

### B. Route BUY and SELL evidence correctly

| | |
|---|---|
| Files | `n7_oneshot_host.py`, `live_oms.py`, `settlement_bridge.py` (reuse) |
| Before | All matches woke entry exposure; SELL never called `note_exit_matched` / `note_exit_filled` on LIVE path |
| After | ENTRY_BUY → ExitSupervisor match wake + settlement matched qty; EXIT_SELL → cumulative HWM `note_exit_matched`; confirmed SELL → `SharedSettlementCoordinator.note_exit_filled` |

### C. Continuous run-to-terminal LIVE loop

| | |
|---|---|
| Files | `exit_supervisor.py`, `n7_live_session.py` |
| Before | Supervision broke after first exit submit (`EXIT_SUBMITTED` → `WAIT` → break) |
| After | Loop continues until `HANDOFF_TERMINAL`, escalate/manual, no-fill, or deadline; `EXIT_SUBMITTED` alone is never success |

### D. Ambiguous entry handling

| | |
|---|---|
| Files | `live_oms.py`, `n7_live_session.py`, `fake_transport.py` |
| Before | Weak single-open-order ownership; AMBIGUOUS skipped exit supervision |
| After | Obligation persisted pre-dispatch; AMBIGUOUS enters supervision; unknown resolution requires client/local correlation (token+side+size alone insufficient) |

### E. FAK orders and $5 cap

| | |
|---|---|
| Files | `transport.py`, `mutation_transport.py`, `n6_live_host.py`, `live_oms.py` |
| Before | Overloaded `amount` for BUY USDC and SELL shares; no `max_spend` |
| After | BUY: `amount` + `max_spend` + `max_price`; SELL: `shares` + `min_price`; `place_market_order()` only; no silent GTC fallback when market order required |

### F. SDK settlement helper

| | |
|---|---|
| Files | `mutation_transport.py` |
| Before | Not used |
| After | `wait_for_order_fill_settlement()` for accepted responses with trade IDs; timeout → `uncertain` + reconciliation (not failure); no SDK types leak past adapter |

### G. Stream recovery and readonly backfill

| | |
|---|---|
| Files | `live_oms.py`, `n7_oneshot_host.py` |
| Before | `run_reconciliation` used mutation transport |
| After | Prefers `readonly_transport` (SdkReadonlyTransport); stream gap blocks new BUY; SELL risk-reducing path remains allowed when sellable evidence exists |

### H. Prior-market crash/restart recovery

| | |
|---|---|
| Files | `n7_oneshot_host.py`, `n7_live_session.py`, lifecycle extras |
| Before | Resume could recon against newly discovered market |
| After | `execution_scope` persisted (market/condition/token); recon/rebuild use stored scope; prior ≠ current market → `MANUAL_INTERVENTION_REQUIRED` before new entry |

### I. Terminal PASS authority

| | |
|---|---|
| Files | `n7_live_session.py`, `exit_supervisor.py` |
| Before | Session overrode baseline flat PASS because supervisor `can_pass` is always false |
| After | Supervisor never grants PASS; baseline/terminal classifier is sole PASS authority; override only when exit remaining is actually unresolved |

### J. Mutation counting and audit evidence

| | |
|---|---|
| Files | `live_oms.py`, `mutation_transport.py`, `n7_live_session.py` |
| Before | Report hardcoded 0/1 from entry status alone |
| After | Pre/post-dispatch ledger on LiveOMS; SdkMutationTransport `mutation_attempt_count`; session uses max of N6 `mutations_dispatched`, OMS ledger, transport ledger |

### K. Typed SDK errors

| | |
|---|---|
| Files | `sdk_errors.py`, `mutation_transport.py`, `user_stream_readonly.py` |
| Before | String-ish timeout classification; 425 not retryable |
| After | `RequestRejectedError.status`; `ENGINE_RESTART` (425) bounded exponential backoff; timeouts → `UNCERTAIN_DISPATCH` (no blind POST retry); rate-limit category preserved |

## 5. Official SDK reuse

| Capability | SDK API used |
|---|---|
| Market orders | `SecureClient.place_market_order` (BUY amount/max_spend/max_price; SELL shares/min_price; FAK) |
| Settlement wait | `wait_for_order_fill_settlement` |
| Errors | `RequestRejectedError.status`, `TimeoutError`, `TransportError`, `RateLimitError`, `UserInputError` |
| Readonly (existing) | `list_open_orders`, `list_account_trades`, `get_balance_allowance`, `get_order` |
| User stream (existing) | `AsyncSecureClient.subscribe(UserSpec)` |

**Not reimplemented:** signing, fee adjustment, FAK construction, WebSocket reconnect client, settlement poller for known trade IDs.

| | |
|---|---|
| Pinned version | `polymarket-client==0.2.0` |
| Dependency changed? | **No** |
| Upgrade proposed? | No — 0.2.0 exposes required market-order, settlement-wait, user-stream, and account-read APIs |

## 6. Lifecycle sequence evidence

Covered by `tests/test_live_lifecycle_minimal_solution.py` production-composition path:

1. Entry submission (armed FakeTransport / LiveOMS ledger)
2. Entry match (HTTP response and/or user-stream UPDATE)
3. Entry confirmation / sellability (settlement axes + balance)
4. Exit submission (`run_bounded_exit_ladder` under supervisor)
5. Exit match (`note_exit_matched` via EXIT_SELL routing)
6. Exit confirmation (`note_exit_filled`)
7. Terminal reconciliation (`classify_n7_terminal` / `session_pass_allowed`)

## 7. Recovery behavior

| Scenario | Behavior |
|---|---|
| Stream gap | Readiness denies entry; REST backfill via readonly transport; no claimed event replay |
| Ambiguous submission | Persist UNKNOWN; supervise; strong ID correlation only; no blind BUY retry |
| Process restart | Lifecycle store + selected_leg + execution_scope + exit supervisor restored |
| Prior-market rollover | Stored market/token used; new market blocked with MANUAL if prior unresolved |

## 8. Tests

### Focused

```text
.venv\Scripts\python.exe -m pytest tests/test_live_lifecycle_minimal_solution.py -q
→ 16 passed
```

Also re-ran remediation / exit / terminal suites (green).

### Full deterministic suite

```text
.venv\Scripts\python.exe -m pytest -q
→ 1051 passed in 141.57s
```

Coverage map for required scenarios 1–16: implemented in `test_live_lifecycle_minimal_solution.py`.

## 9. Safety statement

| Metric | Count |
|---|---|
| Real orders submitted by this task | **0** |
| Real cancellations | **0** |
| Real venue mutations | **0** |
| OBSERVE runs | **0** |
| SHADOW runs | **0** |
| Real-LIVE runs | **0** |

All SDK/network behavior in tests used FakeTransport / MagicMock spies. Mutation arming in tests used FakeTransport only. `$5` risk cap and admission gates were not weakened.

## 10. Remaining limitations and risks

1. Owner must still run the real tiny-LIVE experiment; this task does not claim operational LIVE PASS.
2. Prior-market auto-resume of exit ladder against an old market without operator intervention still ends MANUAL when the newly discovered market differs (fail-closed by design).
3. SDK `retry_after` is not populated by polymarket-client 0.2.0 on `RequestRejectedError` (field read if present; 425 uses local exponential backoff).
4. Settlement helper only applies when the place response already lists trade IDs (SDK contract); delayed fills still require stream/REST.
5. Shared SHADOW/LIVE execution-session refactor remains deferred (per architectural constraint).

## 11. Exact owner command for later tiny-LIVE experiment

**Do not execute in this task.**

> **Note (2026-08-08):** The admission-artifact ceremony has been removed. Current
> operator authorization is `--mode live` + `--live` only. See
> `ADMISSION_SIMPLIFICATION_IMPLEMENTATION_REPORT.md` and
> `operator_tiny_live_runbook.md`. The command block below is historical for this
> report’s original context; do not prepare an admission artifact.

```bash
# Historical note in original report referred to admission artifact prep.
# Current canonical command (owner-run only; not executed by this task):
python -m tyrex_pm.application.cli run \
  --mode live \
  --strategy config/strategies/z_gap.yaml \
  --risk config/risk/tiny_live_5usd.yaml \
  --execution config/execution/polymarket_live.yaml \
  --runtime config/runtime/live_btc_5m.yaml \
  --run-name tiny_live_validation \
  --live
```

Use `operator_tiny_live_runbook.md`. Confirm `--live` reporting fields
(`live_requested` vs `operator_live_armed`) before trusting mutation status.

## 12. Diff summary and `git diff --check`

Primary touched areas:

- `src/tyrex_pm/execution/polymarket/execution_role.py` (new)
- `src/tyrex_pm/execution/polymarket/{transport,live_oms,mutation_transport,fake_transport}.py`
- `src/tyrex_pm/adapters/polymarket/sdk_errors.py`
- `src/tyrex_pm/runtime/{exit_supervisor,n7_oneshot_host,n7_live_session,n6_live_host}.py`
- `src/tyrex_pm/execution/polymarket/user_stream_readonly.py`
- `tests/test_live_lifecycle_minimal_solution.py` (new)

`git diff --check` on the changed LIVE lifecycle files: **clean** (no whitespace errors).

---

*Report generated as part of the LIVE lifecycle minimal solution task. Existing worktree changes were preserved.*
