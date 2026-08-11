# Event-driven execution lifecycle remediation implementation report

**Date:** 2026-08-07  
**Branch:** `rest_project`  
**Starting HEAD:** `28c4d804f0c2927579c35743bfcedb807330d768`  
**Ending HEAD:** same commit + uncommitted remediation worktree (no commit/push)  

---

## 1. Executive result

| Field | Value |
|-------|-------|
| **Overall outcome** | **PARTIAL** — software remediations A–J implemented and deterministic suite green; Phase 9 remains **BLOCKED** on Level 5 |
| Ready for owner review? | **YES** |
| Ready for tiny-live experiment? | **NO** (Phase 9 Level 5 not executed; admission remains false) |
| Real venue mutations | **0** |
| `tiny_live_admitted` | **false** (defaults, templates, dress rehearsal, runtime without reviewed artifact) |
| Tiny-live experiment performed | **NO** |

Deterministic full suite after remediation: **1014 passed**  
Evidence: `remediation_evidence/full_suite_remediation.txt`

---

## 2. Starting state

| Item | Value |
|------|-------|
| Branch | `rest_project` tracking `origin/rest_project` |
| Starting HEAD | `28c4d804f0c2927579c35743bfcedb807330d768` |
| Dirty worktree | Large prior lifecycle implementation already present (Phases 1–10 files uncommitted) |
| Reports reviewed | Implementation plan, phase_01…phase_10 reports, FINAL_IMPLEMENTATION_REPORT.md, PHASE_9 evidence |

Starting worktree: preserved; no reset/revert of unrelated work.

---

## 3. Finding-by-finding remediation

### A — Real continuous authenticated user stream — **PASS**

| | |
|--|--|
| **Original problem** | Real `SdkMutationTransport` got `FakeUserStreamSupervisor(auto_ready_on_start=False)` → `USER_STREAM_INIT_FAILED` |
| **Root cause** | `_build_default_user_stream` treated only `FakeTransport` as stream-capable |
| **Files** | **New** `execution/polymarket/sdk_user_stream.py`; **mod** `normalize.py`, `n7_oneshot_host.py` |
| **Approach** | `SdkAuthenticatedUserStream` uses official `AsyncSecureClient` + `UserSpec`; ready only after auth+subscribe; gap → unready → REST backfill → ready; fail-closed supervisor if construct fails |
| **Production path** | `N7OneShotHost._build_default_user_stream` → `SdkAuthenticatedUserStream` for `SdkMutationTransport`; `ensure_user_stream_ready` before arm |
| **Tests** | `test_real_transport_selects_sdk_authenticated_user_stream`, matrix stream tests, normalize SDK envelope |
| **Limitation** | Live network subscribe proven via SDK-shaped injectable; operator host must have credentials for real WS |
| **Gate** | **PASS** |

### B — Settlement / sellability readonly wiring — **PASS**

| | |
|--|--|
| **Original problem** | Settlement called `get_trades` on mutation transport (no read surface); sole-order uncorrelated attribution |
| **Root cause** | `_default_trade_poller` / balance poller used `self.transport` |
| **Files** | **mod** `n7_oneshot_host.py` (`_default_trade_poller`, `_default_balance_poller`, `apply_settlement_update`) |
| **Approach** | Readonly transport only; filter by selected market/token + owned venue order ids; remove sole-tracking fallback; opposite token → unexpected |
| **Production path** | `_settlement_tick` → `host._default_trade_poller()` → `readonly_transport.get_trades` |
| **Tests** | `test_default_trade_poller_uses_readonly_and_filters`, `test_no_sole_order_uncorrelated_attribution`, matrix delayed settlement |
| **Gate** | **PASS** |

### C — Selected-side identity end to end — **PASS**

| | |
|--|--|
| **Original problem** | N7 hard-coded YES for books/settlement/exit/recon |
| **Root cause** | No persisted selected-leg context from EnterIntent |
| **Files** | **New** `runtime/n7_selected_leg.py`; **mod** host/session books, balance, exit instrument, recon tokens |
| **Approach** | `SelectedLegContext.from_enter_intent`; set once on entry accept; immutable; restore from lifecycle extra |
| **Tests** | `test_yes_and_no_selected_leg_lifecycles`, `test_matrix_yes_and_no_lifecycles_to_confirmed` |
| **Gate** | **PASS** |

### D — FAK and amount semantics — **PASS**

| | |
|--|--|
| **Original problem** | N7 omitted `order_type`; LiveOMS defaulted GTC |
| **Root cause** | Empty `SubmitOrderCommand.evidence` |
| **Files** | **mod** `n6_live_host.py`, `live_oms.py`, `n7_oneshot_host.py` (`require_order_type="FAK"` on arm) |
| **Approach** | N6 sets FAK + BUY USDC amount / SELL shares; OMS refuses missing type when `require_order_type` set; never silent GTC on N7 arm path |
| **Tests** | `test_fak_entry_and_exit_amounts_at_transport`, `test_matrix_fak_buy_usdc_sell_shares_args` |
| **Gate** | **PASS** |

### E — Durable restart / crash reconstruction — **PASS**

| | |
|--|--|
| **Original problem** | New timestamped `run_id` ignored prior unresolved state |
| **Root cause** | No scan of `var/runtime_state/n7/` |
| **Files** | **mod** `persistence/lifecycle_state.py`, `n7_live_session.py` |
| **Approach** | `discover_unresolved_sessions` + `select_session_for_resume`; exactly one → resume; multiple/corrupt → MANUAL; resolved history does not block |
| **Tests** | `test_restart_discovers_prior_state_without_injecting_path`, matrix restart exposure |
| **Gate** | **PASS** |

### F — Reviewed admission artifact — **PASS**

| | |
|--|--|
| **Original problem** | Arm trusted sealed YAML `tiny_live_admitted` |
| **Root cause** | Artifact helpers unused on arm path |
| **Files** | **mod** `n7_admission.py`, `n7_oneshot_host.arm_operator_live`, CLI/tools `--admission-artifact` |
| **Approach** | `validate_admission_for_arm`; YAML flip alone cannot arm; fingerprint match; single-use consumption persisted before `enable_network`; CI/`TYREX_N7_FORBID_LIVE` still refuse |
| **Tests** | yaml flip / valid artifact / single-use reuse tests |
| **Gate** | **PASS** (no production-ready admitted artifact created) |

### G — Production-path integration coverage — **PASS**

| | |
|--|--|
| **Files** | **New** `tests/test_n7_remediation_lifecycle_matrix.py` (15 scenarios) + existing production-path suite |
| **Gate** | **PASS** (30 targeted remediation tests passed) |

### H — Phase 9 acceptance honesty — **BLOCKED (Level 5)**

| | |
|--|--|
| **Action** | Corrected L5 commands; `PHASE_9_CURRENT_STATUS.md` states Phase 9 **BLOCKED**; removed “PASS with blocker” as current success claim; historical reports preserved |
| **Level 5** | Not executed (duration gate); network probes previously OK |
| **Gate** | Phase 9 overall **BLOCKED** |

### I — Reproducible validation — **PASS**

| | |
|--|--|
| Python | 3.11.9 (`.venv`) |
| pytest | 9.1.1 |
| polymarket-client | 0.2.0 |
| Full suite | **1014 passed** |
| Docs consistency | 9 passed |
| `git diff --check` | warnings only (CRLF); no whitespace errors blocking |
| Evidence | `remediation_evidence/versions.txt`, `full_suite_remediation.txt`, `targeted_remediation.txt` |

### J — Mutation-disabled dress rehearsal — **PASS**

| | |
|--|--|
| **Files** | `runtime/n7_dress_rehearsal.py`, `tools/n7_live/run_n7_dress_rehearsal.py`, `tests/test_n7_dress_rehearsal.py` |
| **Result** | `ABORTED_BEFORE_MUTATION` / `tiny_live_not_admitted`; submit_count=0; mutations=0 |
| **Evidence** | `remediation_evidence/dress_rehearsal_run/` |
| **Gate** | **PASS** |

---

## 4. Production lifecycle trace

```text
CLI (n7-live | run --mode live)
  → yaml_config.resolve + write_effective_n7_sealed / n7_bridge
  → validate_admission_for_arm (artifact required for real arm; YAML alone insufficient)
  → select_session_for_resume / discover_unresolved_sessions
  → N7OneShotHost(+ SdkMutationTransport, SdkReadonlyTransport, lifecycle_store)
  → SdkAuthenticatedUserStream.start (AsyncSecureClient + UserSpec)  [ensure_user_stream_ready]
  → SelectedLegContext from EnterIntent (immutable)
  → capture_and_seal_baseline (readonly)
  → rebuild_after_disconnect_or_restart if prior state
  → arm_operator_live (artifact + stream + FAK require_order_type) OR refuse
  → pre-submit book revalidation (n7_presubmit / book_revalidation)
  → N6LiveHost.try_enter → SubmitOrderCommand(evidence order_type=FAK, amount=USDC)
  → LiveOMS.submit → SdkMutationTransport.place_market_order(FAK)
  → HTTP SubmitOrderResult + UserStream events → LiveOMS single writer
  → MatchedExposureProjection → ExitSupervisor
  → SharedSettlementCoordinator via readonly get_trades/balance (selected token)
  → InventorySellable → supervised FAK exit (shares)
  → BaselineAwareReconciliation (readonly)
  → n7_terminal classify → persist lifecycle_state → user_stream.stop
```

Key functions:
- Stream: `SdkAuthenticatedUserStream.start/connect`, `N7OneShotHost._build_default_user_stream`
- Admission: `validate_admission_for_arm`, `N7OneShotHost.arm_operator_live`
- Leg: `SelectedLegContext.from_enter_intent`
- Settlement: `N7OneShotHost._default_trade_poller`, `apply_settlement_update`
- FAK: `N6LiveHost.try_enter/try_exit`, `LiveOMS.require_order_type`
- Restart: `select_session_for_resume`, `LifecycleRuntimeStateStore`

---

## 5. UP and DOWN proof

| Side | Evidence |
|------|----------|
| UP/YES | `test_yes_and_no_selected_leg_lifecycles`, `test_matrix_yes_and_no_lifecycles_to_confirmed` |
| DOWN/NO | same tests assert NO token/book/balance/exit instrument |
| Opposite token | filtered as unexpected; not strategy inventory (`test_default_trade_poller_uses_readonly_and_filters`, unexpected trade matrix) |

---

## 6. FAK and amount-semantics proof

Captured via FakeTransport spy on production-shaped N7 host:

| Call | Asserted |
|------|----------|
| Entry | `order_type=FAK`, BUY `amount` = USDC (limit×qty or fee-inclusive debit), not share qty as amount |
| Exit | `order_type=FAK`, SELL shares = safe sell qty |
| Missing type | refused when `require_order_type=FAK` |
| GTC | not reachable on armed N7 path |

Tests: `test_fak_entry_and_exit_amounts_at_transport`, `test_matrix_fak_buy_usdc_sell_shares_args`

---

## 7. Restart proof

| Crash point | Behavior |
|-------------|----------|
| Prior unresolved under `var/runtime_state/n7/` | Discovered without injecting path; resume single session |
| Multiple unresolved | Fail closed / MANUAL |
| Outstanding exposure | Blocks new entry until resolved |
| Terminal flat history | Does not block new experiment |

Tests: `test_restart_discovers_prior_state_without_injecting_path`, `test_matrix_restart_outstanding_exposure_discover`

---

## 8. Admission proof

| Case | Result |
|------|--------|
| Direct YAML `tiny_live_admitted=true` alone | **REFUSED** |
| Missing artifact | **REFUSED** |
| Fingerprint mismatch / malformed | **REFUSED** |
| Valid artifact (test) | Reaches next safety gate; single-use consumption recorded |
| CI / `TYREX_N7_FORBID_LIVE` | **REFUSED** |
| Production admitted artifact with true flag | **NOT CREATED** by this task |

Dress rehearsal: abort `tiny_live_not_admitted`, submit_count=0.

---

## 9. Phase 9 status

| Level | Status |
|------:|--------|
| 1 | PASS |
| 2 | PASS |
| 3 | PASS |
| 4 | PASS |
| 5 | **BLOCKED** (`duration_gate_not_run_in_phase9_agent`) |
| 6 | PASS |
| **Overall** | **BLOCKED** |

See `PHASE_9_CURRENT_STATUS.md`. Do not interpret historical `COMPLETE_WITH_EXTERNAL_VALIDATION_BLOCKER` as Phase 9 PASS.

Corrected L5 commands: `remediation_evidence/corrected_l5_commands.txt`

---

## 10. Validation results

```text
python          3.11.9
pytest          9.1.1
polymarket-client 0.2.0
PyYAML          6.0.3
websockets      15.0.1

targeted remediation: 30 passed
full suite:           1014 passed
docs consistency:     9 passed
dress rehearsal:      ok=true, stopped_before_submit=true, mutations=0
validate-config live: ok
```

Artifacts under `Docs/implementation/event_driven_execution_lifecycle/remediation_evidence/`.

---

## 11. Changed-file inventory (remediation focus)

| File | New/Mod | Purpose / ownership |
|------|---------|---------------------|
| `execution/polymarket/sdk_user_stream.py` | New | Real authenticated user stream |
| `runtime/n7_selected_leg.py` | New | Selected-leg identity |
| `runtime/n7_dress_rehearsal.py` | New | Mutation-disabled rehearsal |
| `tools/n7_live/run_n7_dress_rehearsal.py` | New | Operator dress-rehearsal CLI |
| `persistence/lifecycle_state.py` | Mod | Unresolved session discovery |
| `runtime/n7_admission.py` | Mod | Artifact validation for arm |
| `runtime/n7_oneshot_host.py` | Mod | Stream/settlement/leg/FAK/admission |
| `runtime/n7_live_session.py` | Mod | Resume + admission artifact path |
| `runtime/n6_live_host.py` | Mod | FAK evidence on submit |
| `execution/polymarket/live_oms.py` | Mod | `require_order_type` |
| `execution/polymarket/normalize.py` | Mod | SDK envelope normalize |
| `application/cli.py` / tools | Mod | `--admission-artifact` |
| `tests/test_n7_remediation_*.py` | New | Production-path coverage |
| `tests/test_n7_dress_rehearsal.py` | New | Rehearsal abort-before-submit |
| Phase 9 status / runbook / evidence | New/Mod | Honest BLOCKED + corrected commands |

---

## 12. Remaining blockers and risks

| Blocker | Path | Safety impact | Next action | Blocks tiny-live? |
|---------|------|---------------|-------------|-------------------|
| Phase 9 Level 5 two-rollover not run | OBSERVE/SHADOW live market-data | Cannot claim operational feed acceptance | Operator runs corrected commands ≥2 rollovers; export evidence | **YES** |
| Real WS subscribe needs operator credentials/network | `SdkAuthenticatedUserStream` | Fail-closed without creds | Run authenticated preflight + dress rehearsal on operator host | Soft (arm already requires stream ready) |
| No reviewed admission artifact with true flag | Admission | Correct — must stay false until review | Owner creates artifact after Phase 9 PASS | **YES** (by design) |

---

## 13. Owner review checklist

1. Read this remediation report + `PHASE_9_CURRENT_STATUS.md`
2. Confirm A–F production traces in code (stream, readonly settlement, selected leg, FAK, restart, admission)
3. Review targeted tests + `remediation_evidence/full_suite_remediation.txt` (1014 passed)
4. Run dress rehearsal: `python tools/n7_live/run_n7_dress_rehearsal.py`
5. Execute Level 5 two-rollover with corrected YAML commands; store evidence
6. Re-run authenticated read-only preflight
7. Only then consider creating a fingerprint-matching admission artifact (`tiny_live_admitted=true`) for **one** experiment
8. Do not authorize continuous live or raise the $5 cap

---

## 14. Final declarations

- **Real venue mutations performed: 0**
- **Tiny-live experiment performed: NO**
- **`tiny_live_admitted`: false**
- **Phase 9 status: BLOCKED** (Level 5)
- **Phase 10 status: readiness support present; experiment not authorized**
- **Ready for owner-authorized tiny-live: NO** — until Level 5 PASS + reviewed admission artifact bound to sealed fingerprint

---

*End of remediation implementation report.*
