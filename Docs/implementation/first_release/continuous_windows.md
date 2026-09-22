# R6 — Continuous windows (deferred extension)

**Status:** Specified, **deferred**. **Not** part of the bounded first-release acceptance gate (R1–R5 + R7).
**Depends on (when built):** R1 drain-on-all-shutdown-paths + `safe_to_open_entry`, R5 session identity.
**Charter:** 2026-09-20 first-release objective asked for consecutive `btc_updown_5m` windows until Ctrl+C. That remains this extension’s requirement. It is **not** mandatory to ship inside the first usable release.

Do **not** implement R6 in the first-release PRs. Do not make R7 depend on `--continue`.

## Why first release stays one-shot

`run_market_data_runtime` already promotes prepared-next (`market_data_runtime.py`) but **stops the process** at `target_window_start + maximum_duration_s`. First release keeps that one-shot behavior. Removing the duration break **alone is not** a complete `--continue` implementation (see below).

Old [`run_continue` plan](../../../old/Docs/Implementation/run_continue/run_continue_plan.md) targets deleted `paired_binary` / `runtime/app.py`. Do not implement it.

## Separate acceptance gate (when product un-defers)

A later PR is **done** only when the orchestration tests and operator rules in this file pass — in addition to R1–R5 still green.

## What “continue” must do (not just drop duration)

Keeping the same `TradingRuntime`:

1. **Window completion ≠ process termination.** A window may end `COMPLETED_NO_ENTRY_SIGNAL` / `COMPLETED_FLAT` / `COMPLETED_NO_FILL` / `COMPLETED_FLAT_WITH_DUST` while the **process** stays up and binds the next market. Process exit is only: operator stop, `--max-windows`, a session that is not `safe_to_open_entry`, `RUNTIME_FAILURE`, or drain failure.
2. **Duration:** one-shot still uses `maximum_duration_s`. Continue mode must **re-anchor** duration per window (or disable the process-level deadline **and** still stop a **stuck** window via existing mandatory/manual deadlines). Simply deleting the `elapsed >= max_duration_s` break leaves a process that never stops on a hung promote and has no per-window bound — **insufficient**.
3. **Promotion:** `can_promote` = no open session or session **`safe_to_open_entry`** (R1). `terminal` including `MANUAL_INTERVENTION` is **not** sufficient. If blocked, do not skip the window; keep draining (R1). If `MANUAL_INTERVENTION`, **do not** promote.
4. **New session_id per window** after `release_terminal()` only when `safe_to_open_entry`. Same journal file, $5 cap per entry.
5. **Ctrl+C:** first signal sets `stop_requested`, **completes current window drain** via the R1 `finally` path (not a pre-break-only callback), does **not** promote, writes reports, exits. Waiting for next window: interruptible; no new bind.

## Continuation-eligible outcomes

After a window’s execution session (or no session):

| Outcome | Promote to next window? |
|---------|-------------------------|
| `COMPLETED_NO_ENTRY_SIGNAL` (no session) | Yes if no prior-session risk |
| `NO_ENTRY_STRATEGY_INPUT_BLOCKED` | Yes (PTB/vol) |
| `NO_ENTRY_RUNTIME_BLOCKED` | **No** (infra) |
| `COMPLETED_NO_DISPATCH` | Yes if `safe_to_open_entry` |
| `COMPLETED_NO_FILL` | Yes if `safe_to_open_entry` |
| `COMPLETED_FLAT` / `COMPLETED_FLAT_WITH_DUST` | Yes |
| `MANUAL_INTERVENTION` | **No** |
| `RUNTIME_FAILURE` / `RUNTIME_DEGRADED` with exposure | **No** |
| Uncertain BUY or SELL | **No** |
| Operator stop | **No** (drain then exit) |

## Window counting (locked)

`windows_attempted` increments when an **active market is bound** (MARKET_BOUND), not when evaluate skips.

`--max-windows N` (required in continue mode for any live use; default in CLI **must be finite** — locked **N=3** if the flag is omitted, to prevent unbounded spend). First-release campaign does not use this flag.

Stop when `windows_attempted >= N` **after** that window has reached a continuation-eligible terminal or operator stop, not mid-POST.

## Reporting

Process `--out-dir` is the session. Append `windows.jsonl` one object per bound window: `window_id`, `outcome`, `session_id`, `mutation_attempts`, `observed_fills` summary (R5 rules, including `prior_session_risk`). Final `run_summary.json` includes `windows: [...]` and `ok=false` if any window was manual/failure/uncertain.

Do not invent PnL. Cumulative `intended_debit` = 5 × count of windows that **attempted** an entry POST (not merely bound).

## CLI (when built)

`--continue` on existing `run` only. `eval` rejects it. `--live` still required. `--max-windows` default 3, minimum 1.

Windows SIGINT: set `stop_requested` on the running loop (Proactor/Selector). Test with a fake signal/event, not only Unix `signal`. Document: second Ctrl+C may abort; first must drain via R1 `finally`.

## Tests (required for the extension gate — fake feeds/clock)

Do **not** stub `run_market_data_runtime` so that duration=off is the only assertion. Drive a **loop** with fake discovery/clock:

| ID | Scenario | Expected |
|----|----------|----------|
| C1 | Window 1 filled then flat; clock crosses `event_end`; `can_promote` true | bind window 2; `windows_attempted==2`; process still running until max-windows |
| C2 | Window 1 not `safe_to_open_entry` | no second bind; drain continues |
| C3 | `--max-windows 1` | no second bind after window 1 eligible terminal; process exit |
| C4 | Stop during wait-for-next (clock before next start) | no bind; report `operator_stop`; R1 drain still runs once in `finally` |
| C5 | One-shot without `--continue` | still breaks on `maximum_duration_s` (regression) |
| C6 | **Windows interruption:** `stop_requested` set during `asyncio.sleep` in the loop | drain in `finally` before feed stop; `coordinator.close` after drain |
| C7 | `MANUAL_INTERVENTION` on window 1 | no promote (`safe_to_open_entry` false); process exit 2 |

C1 is the filled-first-window case. Fake gateway from R4/R1.

## Acceptance (extension only)

1. Window terminal does not by itself exit the process.
2. Duration handling is per-window or equivalently bounded; not “delete the deadline”.
3. Eligible vs blocking outcomes as table; MANUAL is not promotable.
4. Finite `--max-windows`.
5. C1–C7 pass including Windows-style stop.
6. First-release one-shot tests still pass.

## Exclusions until un-deferred

Shipping `--continue` in first-release CLI; R7 continue scenarios; subprocess-per-window; other families; raising $5.
