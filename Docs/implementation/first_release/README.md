# First release — implementation plans (revised)

**Status:** R1–R5 are implemented and their offline release gate has passed. R5.5 startup reconciliation reads standard-binary payouts through `TYREX_POLYGON_RPC_URL` when that variable is set. Neg-risk markets stay unresolved because the pinned SDK does not prove the held token's payout slot. It is not live-validated. R6 remains deferred. F-L2 remains deferred until a strategy that depends on L2 is prepared for release. R7 live validation remains pending.
**Operator / developer API:** [`Docs/latest/`](../../latest/README.md).
**Predecessors:** [grounding_fixes/](../grounding_fixes/README.md), [grounding_framework/](../grounding_framework/grounding_plan.md).
**No AGENTS.md** in this repo; code, tests, and `Docs/latest/` are authoritative.

These files are the accepted plans plus implementation status. Current runtime behavior lives in code, tests, and `Docs/latest/`.

## Supported first-release product

One usable live process on the **existing** host:

| Item | First release |
|------|----------------|
| Host | `TradingRuntime` + `PolymarketAsyncGateway` + `AccountExecutionCoordinator` + reducer + SQLite `execution.sqlite3` |
| Live YAML | `config/runs/z_gap_tiny_live.yaml` |
| Strategy | Registered plugin `z_gap` (typed intents only) |
| Family | `btc_updown_5m` (discovery: **current** slug + `prepare_next` only) |
| Debit | `risk.maximum_total_debit` **5 USDC** (unchanged) |
| Mutations | CLI `--live` only |
| Duration | One-shot `market.maximum_duration_s` (shipped 300 s from prepared window start) |
| Protection | Shipped `protection:` block remains: FAK SL + **resting GTD TP** (maker SELL) |
| Lab | `tyrex-pm eval` fixture replay of `evaluate()` — not a backtester |
| State | Never delete `state_directory` to clear a blocker |

**Registered and technically configurable, not first-release live-validated:** `ask70`, `feature_probe` (config load / lab replay remain allowed). `q_edge` is unregistered. Do not add strategies, connectors, families, or a second host. Do **not** add a loader restriction that refuses non-`z_gap` kinds unless a later task explicitly requires it.

**Deferred from first-release acceptance:** [continuous_windows.md](continuous_windows.md) (`--continue`, R6). [F-L2](feature_input_trust.md#follow-up-f-l2--l2--feature_probe-freshness-deferred) stays deferred until a strategy that depends on L2 is prepared for release. Neither is a first-release pass criterion.

## Locked rules

1. R1–R5 form the implemented foundation. Next implement and validate [R5.5](automatic_startup_reconciliation.md), then resume bounded R7 validation on the new candidate with explicit live authorization. R4 needs R1–R3 behaviour. R6 remains deferred.
2. Reuse existing modules and fake-gateway / lab-fixture patterns. Do not bypass the production path under test (no stubbing `entry_executable=True` to hide a cutoff; no asserting “helper was called” as the only proof).
3. Journal is append-only. Correct wrong fill attribution with **new** evidence; never delete or rewrite original rows.
4. Real venue mutations stay operator `--live`. Tests use fixtures.
5. Do not change Z-Gap `theta` / `z` economics. R2 may raise `tau_min_s` (time band only).
6. Do not revive [`old/Docs/Implementation/run_continue/run_continue_plan.md`](../../../old/Docs/Implementation/run_continue/run_continue_plan.md).
7. Follow existing strategy-plugin, runtime, coordinator, durable-event, reducer, and reconciliation patterns. No parallel OMS, no `if strategy_kind == ...` in generic host modules.

## Verified current host (do not rebuild)

- Plugin `InputContract`, `IndicatorGraph`, generic `evaluate()`, protection overlay independent of `evaluate()`. `z_gap` / `ask70` / `feature_probe` honor `DecisionContext.entry_allowed`.
- Maker GTC POST (`MAKER_LIMIT_POSTED`); resting GTD TP POST as post-only SELL; G4 cancel session-owned ids (never `cancel_all`).
- Recovery replays SQLite. `terminal` includes `MANUAL_INTERVENTION`; `safe_to_open_entry` is false for MANUAL, exposure, live owned ids, and unresolved mutations. Restore keeps sessions that are not safe to open. Recovery-only EXIT may cancel and flatten confirmed MANUAL exposure; it never posts a BUY and never rewrites MANUAL to a completed phase.
- Window promotion already exists; one-shot duration **stops the process** after ~one 5m window. Discovery cannot bind an arbitrary old 5m slug from CLI.
- `run_summary.json` schema 5: outcomes, timeline, `no_entry_diagnosis`, `reproducibility`, fill-completeness, `prior_session_risk`, and `startup_reconciliation`.
- Coordinator serializes `apply()` on `_queue`. `submit()` / `cancel()` share `_mutation_lock`.
- `evidence_timeout_s` starts **after** `post_order` returns (`lifecycle._reconcile_until_entry_resolved`).
- `exit_retry_budget_s` is one wall-clock budget shared across attempts, not `retry_limit × budget`.
- Drain runs once from `run_market_data_runtime` `finally` before feeds, account authority, coordinator, and gateway close.

## Task sequence

| ID | File | First-release? | Depends | Status |
|----|------|----------------|---------|--------|
| R1 | [execution_completion_recovery.md](execution_completion_recovery.md) | **Yes** | — | Implemented (offline). Named F10–F13 / F16 / F18–F19 remain covered by existing maker/cancel/protection tests rather than standalone rows. |
| R2 | [entry_exit_deadlines.md](entry_exit_deadlines.md) | **Yes** | — | Implemented. Tick, consume, and BUY gate share `TimeAuthority.now_corrected_utc`. The strategy window closes `entry_dispatch_margin_s` before the hard cutoff; consume and the BUY gate still use the hard cutoff. |
| R3 | [feature_input_trust.md](feature_input_trust.md) | **Yes** | — | Implemented for z_gap EWMA + book mono age. **F-L2 deferred.** |
| R4 | [zgap_workflow_proof.md](zgap_workflow_proof.md) | **Yes** | R1–R3 | Implemented: four lab packs + connected evaluate→lifecycle + ZL1. |
| R5 | [reproducible_operable.md](reproducible_operable.md) | **Yes** | — | Implemented: reproducibility block, P1–P10, fill-completeness. |
| R5.5 | [automatic_startup_reconciliation.md](automatic_startup_reconciliation.md) | **Yes (new operational requirement)** | R1, R5 | Implemented for standard binary settlement. A copied-state account check on Polygon chain 137 remains blocked: the journal has no wallet binding, one resolved maker session still has execution uncertainty, and two ask70 sessions still have ambiguous exit attempts. Neg-risk stays unresolved. Not ready for a live run. |
| R6 | [continuous_windows.md](continuous_windows.md) | **No (deferred)** | R1, R5 | Not implemented. |
| R7 | [release_validation_campaign.md](release_validation_campaign.md) | **Yes** | R1–R5 + R5.5 offline-green for the next candidate | **Pending.** The first ask70 live attempt emitted one entry and the host rejected it at the hard cutoff (`NO_ENTRY_CUTOFF_BLOCKED`, `ok: true`, zero mutations). That is a safe no-entry. It does not satisfy R7 confirmed entry and exit. |

## Suggested PRs (first release)

| PR | Contents |
|----|----------|
| 1 | R1 |
| 2 | R2 + shipped `tau_min_s` |
| 3 | R3 |
| 4 | R4 fixtures/tests |
| 5 | R5 reporting/CLI |
| next | R5.5 startup reconciliation and guided recovery; staged review boundaries in its plan |
| — | R7 is a campaign run, not a code PR (update its evidence table after the live processes) |
| later | R6 if product un-defers it |

## Remaining product decisions

Ordinary choices (cutoff formula, drain order, live-supported features, campaign allowance, resting-TP live-fill bar) are **locked inside the task files**. Nothing in this pass remains as an either/or that blocks R1 implementation.

1. **Un-defer R6** into the same release train after R1–R5? **Default: no.** First release is one-shot.
2. **Re-run ask70 live SL on this sha?** **Default: excluded** from the required capability set.

## Three acceptance layers (R7)

| Layer | Meaning |
|-------|---------|
| **Plan accepted** | R1–R5 plans are implemented; R5.5 specifies the next operational change and is not implementation evidence. |
| **Offline validation passed** | R1–R5 passed on their candidate. The next candidate also requires the R5.5 acceptance gate, full suite, config checks and R4 eval packs. |
| **Live-release validation passed** | R7 required rows passed, including a **confirmed live entry and exit**. |

Required evidence still pending means **that layer has not passed**. Three processes without a confirmed round trip leave layer B passed and layer C **pending** (not approved). Do not start a fourth process or retune theta/z.

## Offline validation after implementation

```text
python -m tyrex_pm.application.cli run --config config/runs/z_gap_tiny_live.yaml --validate-config
python -m tyrex_pm.application.cli eval --config config/runs/z_gap_tiny_live.yaml --fixture <R4 packs>
pytest -q
```

This revision (hardening pass): 353 pytest passed; all four shipped `--validate-config` ok; four R4 eval packs produce ENTER / SKIP+NO_ECONOMIC_EDGE / WAIT+PTB_NOT_USABLE / EXIT+MARKET_RICH_EXIT (held pack evaluates at τ=180 s, before the 90 s host flatten). `ruff check` on every R1–R5 changed or newly added Python file: clean. Full-repo `ruff check src tests` still reports pre-existing E501/I001/F841 in untouched modules (`planner.py`, `protection/`, older tests, `ewma_volatility.py`, `facts/mappings.py`, `strategies/__init__.py`). Those are baseline findings, not this release. R6 and R7 were not run.

R5.5 review-fix pass, `.venv` Python 3.12.3 with `polymarket-client` 0.5.0 and Ruff 0.16.8 (`pip install -e ".[dev]"` now includes Ruff): 395 pytest passed. The same four configs validate. The same four R4 packs still produce ENTER / SKIP+NO_ECONOMIC_EDGE / WAIT+PTB_NOT_USABLE / EXIT+MARKET_RICH_EXIT at τ=180 s. `.\.venv\Scripts\python.exe -m ruff check` on the R5.5 Python files is clean. `git diff --check` is clean. S07 automatic settlement remains incomplete because SDK 0.5.0 `Market.resolution` has no payout vector. No live account check was run.

R5.5 second-review pass, same `.venv`: 408 pytest passed. The same four configs validate. The same four R4 packs still produce ENTER / ENTRY_CANDIDATE, SKIP / NO_ECONOMIC_EDGE, WAIT / PTB_NOT_USABLE, and EXIT / MARKET_RICH_EXIT at τ=180 s. Ruff on the files touched in this pass is clean. `git diff --check` is clean. CLOB conditional balances are converted from 6-decimal base units, closing open orders are compared, the running coordinator session is included, `PolymarketAsyncGateway.recovery_book` reads `get_order_book`, and a legacy journal can be associated from the live prompt without clearing exposure. S07 still does not run unattended: the standard-binary payout reader calls Polygon CTF `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` only when `payout_eth_call` is injected, and the CLI does not set that. Neg-risk markets stay unresolved. No live account check was run.

R5.5 copied-state account check used a private Polygon HTTPS endpoint. Chain id 137 was verified before any payout call. The placeholder endpoint is still rejected before connect. The original `execution.sqlite3` hash was unchanged and the copied journal received no automatic evidence, because the legacy journal is unbound. The check is blocked by `LEGACY_JOURNAL_UNBOUND`, one `UNEXPECTED_HOLDING`, and four `UNRESOLVED_ATTEMPT` rows. Closed markets are queried again with `closed=True` when the open query is empty. A balance-only holding can take `outcomeIndex` from the matching closed position. A recorded dust residual that still matches the venue balance is not a new holding. Neg-risk stays unresolved. No live order was sent.
