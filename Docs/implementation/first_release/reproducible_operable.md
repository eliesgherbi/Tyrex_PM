# R5 — Reproducibility and operation

**Status:** Implemented (2026-09-20). **First-release required.** Reproducibility is captured before `TradingRuntime.create`. Normal and emergency reports carry identity/hashes; emergency omits the huge `effective_config` dump. `COMPLETED_NO_DISPATCH` is a confirmed no-mutation outcome. `prior_session_risk` can be true with `state is None`. MANUAL never presents empty fills as proven zero. P1–P10 JSON tests landed.
**Depends on:** nothing (R6 is not required). Do not mention `--continue` as a first-release operator step.
**Reuse:** `build_run_report` / `write_emergency_report`, CLI `_config_payload`, `TradingRuntime.create` `run_instance_id`.
**Bounded scope:** identity, collision refusal, and honest fill/recon language. Not remote upload. Not invented fees.

## Current behavior (verified)

`RUN_STARTED` (`trading_runtime.py`): run name, kind, out dir, live flag. No git, no Python, no dependency versions, no config.

`build_run_report` schema 4: execution projection, `no_entry_diagnosis`, `validation_scope`. Confirmed trades in `execution.trade_ids` / reducer state are **observed fills**, not a claim that recon is complete (`reconciliation_complete` is separate and often ignored in summaries).

`no_entry_diagnosis` is emitted when `source.state is None` — a run with **no execution session**. That is not the same as a submitted-unfilled session and not the same as “no leftover from a prior process.”

CLI `_config_payload` omits `strategy.parameters` and `protection`. File bytes hash ≠ loaded dataclass (defaults, plugin fill-in).

`--out-dir` can overwrite `run_summary.json` after the process already composed the gateway: collision check is missing **before live activity**.

`write_emergency_report` has `run_instance_id` and `configured_run_name` but no git/config/python.

No fee field on `TradeStatusObserved`. `_position_cost` is fill notional, not fees.

Restore currently drops `MANUAL_INTERVENTION` because it is `terminal` (R1). Reports that say “no session / complete fills” can hide prior-session risk.

## Locked reporting shape

Top-level `reproducibility` on **normal and emergency** reports (emergency: omit huge `effective_config` if dump fails, but keep ids/hashes already computed):

| Field | Rule |
|-------|------|
| `package_version` | `tyrex_pm.__version__` always |
| `python_version` | `sys.version` (full string) |
| `dependencies` | `{ "polymarket-client": ..., "PyYAML": ..., "websockets": ... }` via `importlib.metadata.version`; **null** per key if missing, never invent |
| `git_revision` | `git rev-parse HEAD` or **null** + `git_revision_error` |
| `git_dirty` | bool or **null** if git missing |
| `config_path` | CLI path |
| `source_config_sha256` | hash of **file bytes** as read |
| `effective_config` | JSON of loaded `TradingRunConfig` (host + `strategy.kind` + `strategy.parameters` + `protection` if any). No env, no private keys |
| `effective_config_sha256` | hash of canonical JSON of `effective_config` (sorted keys). **Separate** from `source_config_sha256` |
| `cli_flags` | `{live, out_dir}` only (no continue in first release) |
| `run_instance_id` | existing UUID; required on emergency reports |

Capture git/python/deps **once** at process start **before** `TradingRuntime.create` network. If create fails, emergency report still includes that blob.

Actionable recovery identifiers (always present when known, else null — never `"unknown"`): `run_instance_id`, `session_id`, `market_id`, `window_id`, `token_id`, inspect `host_action` from R1 when a blocking restored session exists.

### Three outcome meanings (locked)

| Meaning | When | Report shape |
|---------|------|----------------|
| **No trading occurred** | This process never opened an execution session (`state is None`) **and** did not POST | `outcome` like `COMPLETED_NO_ENTRY_SIGNAL` / blocked-no-session; `observed_fills=[]`; `observed_fills_complete=true` **for this process only** |
| **Confirmed outcome** | Session reached `COMPLETED_NO_FILL` / `COMPLETED_FLAT` / `COMPLETED_FLAT_WITH_DUST` / `COMPLETED_NO_DISPATCH` with recon complete and no unresolved BUY **or SELL** attempt | fills list (maybe empty); `observed_fills_complete=true`; `reconciliation_complete=true` |
| **Outcome remains uncertain** | Ambiguous POST, match awaiting confirmation, live owned ids, incomplete recon, `MANUAL_INTERVENTION`, or projection failed | `observed_fills_complete=false`; `reconciliation_complete` false or null; do **not** present empty fills as proven zero |

### Fill completeness (locked)

```text
summary.observed_fills: list | null
summary.observed_fills_complete: bool
summary.reconciliation_complete: bool | null
summary.prior_session_risk: bool
```

**No new session does not prove there is no unresolved prior-session risk.** `prior_session_risk` is true if this process **blocked** new entry because of an unsafe restored session (R1 `UNRESOLVED_PRIOR_MARKET_SESSION` / `MULTIPLE_RESTORED_SESSIONS` / same-market MANUAL) or a **scoped** leftover on a retained session’s market/token (including F20b–F20d: current window clear, old window exposed/stale/uncertain). `foreign_inventory_absent` (R1 F20) is audit trail, not `prior_session_risk` — and that inspect is allowed only after a fresh complete observation of the **retained** market and token, not a clear snapshot of the new window. A no-entry report with `prior_session_risk=true` is **not** a clean no-trading claim.

Completeness must account for **pending or ambiguous SELL** evidence as well as BUY: live resting TP, unmatched exit POST, `has_unresolved_mutation_attempt` on an EXIT order, `entry_match_awaiting_confirmation`, or SELL trades not yet CONFIRMED. Empty BUY list with a working SELL is `observed_fills_complete=false`.

| Situation | `observed_fills` | `observed_fills_complete` | `reconciliation_complete` | `prior_session_risk` |
|-----------|------------------|---------------------------|---------------------------|----------------------|
| No session this process, no unsafe restored, account prior_scope_clear | `[]` | true (this process) | null | false |
| No session this process, restored MANUAL still blocking (same market, scoped leftover, stale/missing old-market read, or journal uncertainty) | `[]` | false | null | true |
| Session, journal projected, complete flat/no-fill, no unresolved BUY/SELL | confirmed trades | true | true | as restored/account |
| Session MANUAL or ambiguous BUY **or SELL** | list or `[]` | **false** | false or null | true if leftover |
| Projection failed | **null** | false | null | null or true if restore failed open |

Empty list + `observed_fills_complete=true` on `MANUAL_INTERVENTION` with ambiguous POST is **forbidden**.

`fees`: **null** always until evidence has a real fee field (today: none). Do not use `fee_reserve_rate`.

`residual_shares`: dust or confirmed leftover string, else **null** if unknown (manual/incomplete).

`open_venue_order_ids`: from state or **null** if projection failed.

`errors`: list (maybe empty), never a placeholder sentence.

No PnL field.

## Collision before live

In `cli.py` `_run`, **after** config load, **before** `asyncio.run(run_trading_runtime)`:

If `--out-dir` is set and `out_dir / "run_summary.json"` exists → print error, exit 2, **no** runtime create.

Default stamped `report_directory / run_name_utc` is unique; no extra check required unless the stamp collides (refuse if exists).

Do not create the gateway first.

## Operator procedure (land in `Docs/latest/running.md`)

1. **Start:** `--validate-config`; `eval` on R4 entry pack; `--live --out-dir <empty-or-new> --dotenv .env`.
2. **Inspect:** `reproducibility.*`, `summary.observed_fills` vs `observed_fills_complete` vs `prior_session_risk`, `outcome`, `no_entry_diagnosis` only when there was no session. If MANUAL or `prior_session_risk`: sqlite `execution_events` + venue open orders/balances using R1 identifiers.
3. **Stop:** Ctrl+C once **while the runtime is still in the market-data loop** if testing interruption; wait for R1 drain + report. Do not delete sqlite.
4. **Recover:** R1 prior-session table. Same `state_directory`. Never delete state. Do not bind an old 5m slug (unsupported).

## Fixture scenarios

| ID | Case | Expected |
|----|------|----------|
| P1 | git missing | `git_revision: null`, not `"unknown"` |
| P2 | file hash ≠ effective hash (e.g. after load defaults) | both present and **not equal** is allowed; both present |
| P3 | no fee evidence | `fees: null` |
| P4 | ambiguous session (BUY or SELL) | `observed_fills_complete is false` |
| P5 | no-fill terminal, recon complete, no prior risk | fills `[]`, complete true, recon true, `prior_session_risk` false |
| P6 | `run_summary.json` already in `--out-dir` | exit 2 **before** gateway (fake: patch create not called) |
| P7 | `write_emergency_report` | contains `run_instance_id`, `python_version`, `source_config_sha256` if already computed |
| P8 | effective_config has no `PRIVATE_KEY` / dotenv keys | grep payload |
| P9 | no session this process + restored MANUAL | `prior_session_risk true`; complete false or not a clean no-trading claim |
| P10 | no session, no restored unsafe, account clear | no-trading meaning; complete true for this process |

Do not assert a helper was called; parse the JSON.

## Acceptance

1. Two hashes + effective dump + python + listed deps.
2. Observed fills ≠ implied recon completeness; SELL ambiguity counts.
3. No session ≠ no prior-session risk.
4. No-trading / confirmed / uncertain are distinguishable.
5. Unknown fees/residual stay null.
6. Collision refused before live.
7. Emergency reports keep run identity and recovery ids when known.

## Exclusions

Remote artifact upload; inventing fees; `--continue` in first-release CLI help as required; secret scanning beyond not dumping env.
