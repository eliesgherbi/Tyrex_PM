# R2 — Entry and exit deadlines

**Status:** Implemented (2026-09-20). **First-release required.** Tick, consume, and the BUY gate share `TradingRuntime.time_authority.now_corrected_utc`. `_current_entry_tau_s()` returns `None` (fail closed) when that authority is missing. SELL and protective exits stay independent of the entry cutoff. Shipped YAML `tau_min_s` is 105.
**Depends on:** nothing (parallel with R1). Shipped `tau_min_s` changes (time band only).
**Reuse:** `entry_tau_bounds()`, `CapabilityController.entry_window_open`, `FinalExecutionGate`, `load_trading_run_config`, existing `DecisionContext.entry_allowed` (already checked by `z_gap` / `ask70` / `feature_probe`). Do **not** wire unused `compute_lifecycle_deadlines` or `book_revalidation.entry_allowed_until` as a second authority.
**Bounded scope:** one effective BUY cutoff at evaluate, intent consume, and final gate. Not a timing guarantee that flatten always retains 45 s. Not a merge of strategy `flatten_before_event_end_s` into the host floor.

## Current behavior (verified)

Shipped z_gap/ask70 YAML: `tau_min_s=10`, `mandatory_exit_before_end_s=90`, `manual_deadline_before_end_s=45`, `evidence_timeout_s=15`, `prepare_budget_ms=2000`, `exit_retry_budget_s=30`, `exit_retry_limit=3`.

`on_async_tick` (`trading_runtime.py` ~1283–1286):

```text
entry_window_open = entry_enabled and tau_min_s <= tau <= tau_max_s
```

Entry stays open **inside** the flatten window (e.g. tau=50). `evaluate()` can emit `EnterIntent` when `entry_allowed` is still true. `FinalExecutionGate` has **no tau check** (`final_gate.py`). It uses cached `caps.entry_executable`, which includes `entry_window_open` updated on the 1 Hz tick — so a BUY can still authorize after prepare if the tick has not flipped.

Loader (`run_config.py`) does not compare plugin tau to mandatory exit.

Z-Gap `flatten_before_event_end_s=20` is a **strategy** time-exit; host flatten at 90 always wins. Keep both roles (`config_guide.md` lookalikes). Do not merge.

`exit_executable` does **not** require `entry_window_open`. Final gate SELL does not use the entry ask path. **Keep it that way.**

`lifecycle._reconcile_until_entry_resolved` starts `evidence_timeout_s` **after** `coordinator.submit` returns (POST completed or raised). It does **not** cover prepare, POST network time, or later cancel/recon.

`submit_exit` (`lifecycle.py` ~252–254):

```text
deadline = loop.time() + self.policy.exit_retry_budget_s
while attempts < self.policy.exit_retry_limit and loop.time() < deadline:
```

The 30 s budget is **shared** across attempts. It is not `3 × 30`. Cancel, REST recon, and sleeps consume the same clock.

Prepare/sign is bounded by `prepare_budget_ms`. The POST itself has no additional host “subsecond venue” bound. Cancel delay is 0.30 s plus a retry; recon pulses at 0.25 s; those can exceed any assumed short duration.

## Timing inventory (shipped)

| Step | Bound in code | When it actually starts |
|------|----------------|-------------------------|
| Prepare/sign | `prepare_budget_ms` = 2 s; abandon, no POST | Before gate |
| Intent→POST fuse | `maximum_candidate_age_ms` = 5 s | Gate |
| POST HTTP | SDK / network; **no** host subsecond guarantee | Inside `submit()` before timeout clock |
| Entry evidence wait | `evidence_timeout_s` = 15 s | **After** POST returns |
| Cancel delay | `cancel_delay_s` = 0.30 s + 1 retry | Flatten/OCO |
| REST recon pulse | `reconciliation_interval_s` = 0.25 s | During evidence wait |
| Flatten FAK retries | `exit_retry_budget_s` = 30 s **shared**, `exit_retry_limit` = 3 | After `tau <= 90` |
| Manual stop | `manual_deadline_before_end_s` = 45 | If still exposed |

Flatten **window length** = 90 − 45 = **45 s** of automated exit **if** the process still has exposure and remaining wall time when flatten starts. That window already contains the shared 30 s retry budget. Do **not** add `exit_retry_budget_s` into the entry floor (double-count). Do **not** add `prepare_budget_ms` into a **post-prepare** cutoff (107 s rejected: mixed a pre-gate budget into a post-gate check).

## Locked policy

**Clocks:** tau = `(event_end - time_authority.now_corrected_utc()).total_seconds()` — same as the tick. Final gate uses that callable, not `datetime.now`.

```text
host_safety_floor_s = mandatory_exit_before_end_s + evidence_timeout_s
# shipped numbers: 90 + 15 = 105

effective_entry_cutoff_s = max(plugin_tau_min_s, host_safety_floor_s)
```

**Same `effective_entry_cutoff_s` at all three sites:**

| Check | Rule |
|-------|------|
| Load | Plugin `tau_min_s >= host_safety_floor_s`. `tau_max_s > tau_min_s`. Fail `RunConfigError`, no silent clamp. After a valid load, `plugin_tau_min_s == effective_entry_cutoff_s` when the plugin is not stricter than the floor; if the plugin is stricter, cutoff is the plugin value. |
| Tick `entry_window_open` | `entry_enabled and effective_entry_cutoff_s <= tau <= tau_max_s`. `evaluate()` sees this via `entry_allowed` / `entry_executable` (plugins already honor `context.entry_allowed`). |
| Consume `EnterIntent` | If tau `< effective_entry_cutoff_s`, `INTENT_BLOCKED`, do not `lifecycle.open` / do not POST. |
| **Final gate BUY** (market buy **and** limit BUY) | If tau `< effective_entry_cutoff_s` → fail `ENTRY_CUTOFF_PASSED`, `COMPLETED_NO_DISPATCH`, no POST |
| Final gate SELL (FAK or limit) | **No** entry-cutoff check |
| `tau <= mandatory_exit_before_end_s` | Host flatten / R1 drain only; never new BUY authorization |

Do not implement the cutoff as `if strategy_kind == "z_gap"`. Helper lives in `run_config.py` (or capabilities) and is reused by tick, consume, and gate.

### 105 s is operational policy, not a timing guarantee

With shipped 90+15, YAML `tau_min_s` becomes **105** (or higher if the plugin wants a stricter band). That is the **intended** operator window: refuse new BUY once remaining time cannot hold a full `evidence_timeout_s` **after POST returns** before mandatory flatten.

It does **not** guarantee:

- that prepare + POST return in subsecond time
- that cancel/recon stay inside 0.3–0.6 s
- that 45 s of flatten remain after a slow POST
- that `evidence_timeout_s` overlaps only the pre-flatten region if POST is slow

**When submission or reconciliation consumes the remaining margin:**

- Gate already passed: do not POST a second BUY. R1 recon runs until `evidence_timeout_s` after POST return. Fill → flatten with whatever time remains (mandatory flatten / manual deadline / shared exit budget). No fill proof → `COMPLETED_NO_FILL`. Unproven → `MANUAL_INTERVENTION`.
- Consume/gate tau already below cutoff: no POST (`COMPLETED_NO_DISPATCH` / `INTENT_BLOCKED`).
- In-flight prepare when tau crosses cutoff: do **not** cancel HTTP/2 `create_market_order` (G2). Gate refuses POST.
- Exit path: the **remaining** shared `exit_retry_budget_s` is used; attempts stop when either the attempt count or the **same** deadline is hit. If recon/cancel ate the budget, mark manual / existing `exit_retry_budget_exhausted` — do not extend the budget.

Do not justify safety using assumed subsecond venue responses.

**Changed trading window** (300 s `btc_updown_5m`, `tau_max_s=290` unchanged) when YAML `tau_min_s=105` (cutoff = 105):

| Elapsed from `event_start` | tau | What is allowed |
|----------------------------|-----|-----------------|
| 0–10 s | 300–290 | No entry (`tau_max`) |
| 10–195 s | 290–105 | Entry if strategy+infra ready |
| 195–210 s | 105–90 | No new BUY (cutoff). Existing exposure: strategy/protection exits still allowed |
| 210–255 s | 90–45 | Host `MANDATORY_FLATTEN` / R1 drain. No BUY |
| 255–300 s | 45–0 | `MANUAL_INTERVENTION` if still exposed |

If YAML `tau_min_s=120` (stricter than 105), the no-new-BUY boundary is **120**, not 105, at evaluate, consume, and gate.

Shipped YAML **must** set `tau_min_s` to **105** (or higher): `z_gap_tiny_live.yaml`, `ask70_protection_tiny_live.yaml`, `ask70_maker_tiny_live_danger.yaml`, `feature_probe_catalog.yaml`. Tests that **load those files** must follow. Unit tests that construct `Ask70Config(tau_min_s=10)` in isolation may keep 10 **only** if they do not go through `load_trading_run_config` with lifecycle 90/15.

Do not change `theta_*` / `z_min` / `z_max`.

## Smallest change

1. `host_safety_floor_s` + `effective_entry_cutoff_s(config)` in `run_config.py`; validate plugin bounds.
2. Tick, consume, and BUY gate all use `effective_entry_cutoff_s`.
3. `FinalExecutionGate` receives `entry_tau_s: Callable[[], float | None]` (None if no `event_end` → fail closed on BUY). BUY: tau `< cutoff` → `ENTRY_CUTOFF_PASSED`. SELL: skip.
4. Update shipped YAML `tau_min_s: 105`.

## Fixture scenarios

Reuse `test_final_execution_gate.py` and `test_unified_runtime.py` tick tests. Drive **real** `FinalExecutionGate.__call__`, not a patched `entry_executable=True`.

| ID | Setup | Expected |
|----|--------|----------|
| D1 | Load current tiny-live after tau_min=105 | `--validate-config` equivalent: `load_trading_run_config` succeeds |
| D2 | YAML tau_min=10, lifecycle 90/15 | `RunConfigError` |
| D3 | tau_max ≤ cutoff | `RunConfigError` |
| D4 | Tick tau=50, plugin would have allowed 10–290 | `entry_window_open is False`; `evaluate()` does not ENTER (`entry_allowed` false) |
| D5 | Tick tau=120, tau_min=105 | open (other caps aside) |
| D6 | Limit/market BUY, after prepare, tau=100, cutoff=105 | gate fail `ENTRY_CUTOFF_PASSED`; `post_order` not called; `COMPLETED_NO_DISPATCH` |
| D7 | BUY, tau=110, cutoff=105 | gate may pass (other checks equal) |
| D8 | `MarketSellOrderSpec` / limit SELL, tau=40, `exposed=True`, books OK | gate **not** `ENTRY_CUTOFF_PASSED`; exit still subject to book/price rules |
| D9 | FlattenIntent path with `entry_window_open=False` | lifecycle still submits SELL (capability `exit_executable`) |
| D10 | Plugin `tau_min_s=120`, host floor=105; evaluate ENTER at tau=122; prepare runs until tau=118 | consume and/or gate refuse; no POST; cutoff used is **120** at tick, consume, and gate |

D10 is required: strategy cutoff stricter than host floor, time crosses the **strategy** cutoff during preparation.

Do not assert “floor function returned 105” as the only test; D6/D8/D10 are the acceptance set.

## Failure / recovery

Prepare already running when tau crosses cutoff: do **not** cancel HTTP/2 `create_market_order`. Gate refuses POST → `COMPLETED_NO_DISPATCH`. Session may exist; R1 no-fill/no-dispatch terminals apply. No retry BUY below cutoff.

If POST returns so late that flatten margin is gone: R1 completion rules apply; do not claim the 45 s flatten window was preserved.

## Acceptance

1. One formula: `effective_entry_cutoff_s = max(plugin_tau_min_s, mandatory_exit_before_end_s + evidence_timeout_s)` at evaluate (via `entry_allowed` + plugin tau band), consume, and BUY gate.
2. Incompatible YAML fails at load.
3. BUY cannot dispatch below the cutoff **after prepare**, including D10.
4. SELL/flatten not blocked by the entry cutoff (D8/D9).
5. Docs state 105 s as operational policy; evidence timeout starts after POST returns; exit budget is shared; remaining-margin behavior is R1 completion, not a silent extra POST.
6. Operator-visible window table landed in `Docs/latest/config_guide.md` when coding.

## Exclusions

Merge strategy `flatten_before_event_end_s`; resurrect `compute_lifecycle_deadlines` as live authority; change 5m family length; raise $5; treat 105 as an enforced end-to-end latency SLA.
