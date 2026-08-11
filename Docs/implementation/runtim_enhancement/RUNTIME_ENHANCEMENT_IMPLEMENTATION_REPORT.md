# LIVE Runtime Enhancement — Implementation Report

**Date:** 2026-08-08  
**Branch:** `rest_project` (working tree preserved; no commit/push)  
**Plan:** `Docs/implementation/runtim_enhancement/runtime_impl_plan.md`  
**Outcome:** **COMPLETE** — required Phases 1–7 and Phase 10 implemented; deterministic suite green; owner tiny-LIVE not run.

## 1. Overall outcome

COMPLETE for the agent implementation mandate (Phases 1–7 + 10). Phase 8 (DecisionScheduler) and Phase 9 (SQLite/persistence trim) remain deferred per plan.

Real venue and chain mutations during this task: **zero**.

## 2. SDK spike and pin

See `SDK_0_5_0_COMPATIBILITY_REPORT.md`.

| Item | Result |
|------|--------|
| Spike environment | Isolated temp venv |
| Spike verdict | **PASS** |
| Final project pin | `polymarket-client==0.5.0` |
| Hot-path order API | `create_market_order` + `post_order` (not `place_market_order`) |
| Metadata | Official `AsyncOrderMetadataCache` (0.5.0) |

## 3. Architecture implemented

Evolved existing N7 / N6 / LiveOMS path (no parallel engine, no second OMS):

- `prepare_execution_resources` before strategy entry evaluation
- `LiveReadinessController` + capability matrix gates `entry_eval_ready`
- Lazy post-candidate init forbidden unless `TYREX_ALLOW_LAZY_LIVE_INIT=1`
- LiveOMS create → final gate → durable pre-dispatch → post
- User-stream evidence marshalled through `LiveOmsWriter` (dual-client fallback)
- Event-driven exit supervision with REST as bounded fallback
- Token-scoped book DESYNC + REST snapshot restore without BindingFeed reconnect

## 4. Event-loop / client ownership

| Plane | Ownership |
|-------|-----------|
| Main asyncio loop | Compose + prepare + OMS writer drain + exit supervision |
| Mutation / account HTTP | Loop-prepared `AsyncSecureClient` via `SdkMutationTransport` |
| UserSpec stream | Dedicated stream supervisor (existing SDK thread path) with **mandatory** marshal into `LiveOmsWriter` on the main loop |
| LiveOMS / ExitSupervisor / readiness / persistence | Main-loop writer only (no direct cross-thread OMS mutation when writer bound) |

Never share one `AsyncSecureClient` across loops. Dual-client stream+HTTP is the documented fallback and is what production prepare uses today, with marshalling.

## 5. Exact hot-path ordering

```text
candidate_selected
→ plan / risk (N6)
→ create_market_order (sign only)          [sdk_prep_*]
→ newest BookView + final_gate            [final_gate_*]
→ atomic durable pre-dispatch (§7.7 fields in lifecycle extra)
→ post_order                              [http_post_started → response_received]
→ LiveOMS apply HTTP result
→ user-stream evidence via LiveOmsWriter (may race ahead of HTTP)
→ exit wake on matched / sellable
→ supervise until flat | no-fill | MANUAL | deadline
```

If final gate fails after sign: discard signed order, **zero POST**, no mutation_dispatch increment, obligations resolved cleanly.

## 6. Files changed (by phase)

### Phase 1 — SDK + telemetry
- `pyproject.toml` → `polymarket-client==0.5.0`
- `src/tyrex_pm/runtime/n7_latency.py`
- `Docs/implementation/runtim_enhancement/SDK_0_5_0_COMPATIBILITY_REPORT.md`

### Phase 2 — preparation + ownership
- `src/tyrex_pm/runtime/n7_prepare.py`
- `src/tyrex_pm/runtime/n7_readiness.py`
- `src/tyrex_pm/runtime/oms_writer.py`
- `src/tyrex_pm/runtime/n7_live_session.py`
- `src/tyrex_pm/runtime/live_zgap_compose.py`
- `src/tyrex_pm/runtime/n7_oneshot_host.py` (writer bind + stream marshal)

### Phase 3 — exact scope / durability
- `src/tyrex_pm/persistence/lifecycle_state.py` (schema v3; unscoped blocking → MANUAL)
- `src/tyrex_pm/execution/polymarket/live_oms.py` (atomic pre-dispatch record before POST)
- `src/tyrex_pm/runtime/n7_live_session.py` / `n7_oneshot_host.py` (no YES recovery fallback)

### Phase 4 — create/sign → gate → durable → post
- `src/tyrex_pm/execution/polymarket/mutation_transport.py` (`create_signed_market_order` / `post_signed_order`; sync path prefers create+post)
- `src/tyrex_pm/execution/polymarket/live_oms.py` (`submit(..., final_gate=, latency=)`)
- `src/tyrex_pm/runtime/n6_live_host.py` (capability gate + final_gate + latency)

### Phase 5 — no-fill / ambiguity
- `src/tyrex_pm/execution/polymarket/live_oms.py` (FAK no-fill → resolve session)
- `src/tyrex_pm/runtime/n7_terminal.py` (`PASS_N7_NO_FILL`)

### Phase 6 — stream / settlement / exit
- `src/tyrex_pm/runtime/n7_oneshot_host.py` (`reconnect_with_backoff` + capability flips)
- `src/tyrex_pm/runtime/exit_supervisor.py` (event-primary; REST fallback default 2s)
- `mutation_transport.py` (no inline settlement wait on submit)

### Phase 7 — book DESYNC
- `src/tyrex_pm/market_data/book_store.py` (DESYNC without raise; `apply_authoritative_snapshot`)
- `src/tyrex_pm/market_data/book_feed.py` (`_resync_desynced_tokens` after events)

### Phase 10 — tests / docs
- `tests/test_runtime_enhancement_core.py`
- Updated: `test_n7_terminal_safety.py`, `test_exec_lifecycle_recovery.py`, `test_live_lifecycle_minimal_solution.py`, `test_exec_lifecycle_obligations.py`, remediation discovery tests
- This report + `TINY_LIVE_READINESS_PACKET.md` + `full_suite_result.txt`

### Deferred
- Phase 8 DecisionScheduler — not required for correctness
- Phase 9 SQLite — no measurement of critical-write budget violation

## 7. Tests added / extended

`tests/test_runtime_enhancement_core.py` covers:

- FAK no-fill classifier + `PASS_N7_NO_FILL`
- Ambiguous ≠ no-fill
- Primary dispatch latency span ordering
- Allowance readiness blockers
- Crossed delta → DESYNC; authoritative snapshot restore
- OMS writer thread marshal
- Signed-order discard with zero POST
- Event-loop heartbeat during slow async POST
- Unscoped blocking lifecycle → MANUAL
- create+post path never calls `place_market_order`
- Exit capability false when stream not ready

Related suites updated for schema v3, no-fill PASS policy, and settlement-helper background-only behavior.

## 8. Focused and full-suite results

| Suite | Result |
|-------|--------|
| Focused enhancement + related | green (see session runs) |
| **Full deterministic** `pytest tests -q` | **1076 passed** in ~110s |
| Artifact | `Docs/implementation/runtim_enhancement/full_suite_result.txt` |

OBSERVE / SHADOW / LIVE were **not** used as an acceptance ladder.

## 9. Latency evidence (deterministic)

`test_latency_primary_dispatch_span` asserts:

- `candidate_selected → http_post_started` primary metric
- Ordered marks through `sdk_prep_*`, gate, durable, `http_post_*`
- `http_post_started` absent when signed order is discarded

Production marks `http_post_started` only around SDK `post_order` (LiveOMS create/post path and transport helpers).

## 10. Venue / chain mutations

| Action | Count |
|--------|-------|
| `--live` runs | 0 |
| Real `post_order` / place | 0 |
| ERC-20 / ERC-1155 approvals | 0 |
| Blockchain transactions | 0 |
| Claimed operational LIVE PASS | no |

## 11. Deviations from the plan

1. **User stream remains dual-client** (dedicated stream loop/thread + main-loop HTTP client) rather than a single shared main-loop `AsyncSecureClient` for UserSpec. Mitigation: `LiveOmsWriter` marshalling is required when prepare binds the writer — preserves single-writer OMS semantics.
2. **N6 still performs an early book revalidation** before OMS submit, plus LiveOMS `final_gate` after create/sign when the create/post path is used. Defense in depth; after-sign gate is authoritative for discard-before-POST.
3. **Phase 8/9 deferred** as allowed.
4. Some §13 race/crash scenarios remain covered by existing lifecycle suites rather than every row duplicated under `test_runtime_enhancement_core.py`.

## 12. Remaining blockers before owner tiny-LIVE

1. Operator must ensure **collateral + conditional allowances** out-of-band (Tyrex will not approve).
2. Clear or manually resolve any prior `var/runtime_state/n7/` blocking sessions.
3. Owner reviews this report + readiness packet, then runs the exact Git Bash command below.
4. First live experiment remains owner-operated; agent must not claim LIVE PASS.

## 13. Exact owner Git Bash command

```bash
cd /c/Users/elies.gherbi/Desktop/work/pm/Tyrex_PM

python -m tyrex_pm.application.cli run \
  --mode live \
  --live \
  --strategy config/strategies/z_gap.yaml \
  --risk config/risk/tiny_live_5usd.yaml \
  --execution config/execution/polymarket_live.yaml \
  --runtime config/runtime/live_btc_5m.yaml \
  --dotenv .env \
  --reporting config/reporting/full.yaml \
  --max-duration-s 300 \
  --out-dir var/runs/z_gap/tiny_live_post_enhancement_$(date -u +%Y%m%dT%H%M%SZ)
```

## 14. Evidence / report paths

| Artifact | Path |
|----------|------|
| Implementation plan | `Docs/implementation/runtim_enhancement/runtime_impl_plan.md` |
| SDK compatibility | `Docs/implementation/runtim_enhancement/SDK_0_5_0_COMPATIBILITY_REPORT.md` |
| This report | `Docs/implementation/runtim_enhancement/RUNTIME_ENHANCEMENT_IMPLEMENTATION_REPORT.md` |
| Readiness packet | `Docs/implementation/runtim_enhancement/TINY_LIVE_READINESS_PACKET.md` |
| Full suite log | `Docs/implementation/runtim_enhancement/full_suite_result.txt` |
