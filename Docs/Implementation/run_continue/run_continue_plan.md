# Tyrex_PM run_continue Implementation Plan

## 1. Executive Summary

Tyrex_PM today exposes a one-shot live command (`tyrex-pm run`) that resolves a single Polymarket event URL, boots live infrastructure, runs paired-binary (or other wired strategy kinds) once, writes per-run artifacts under `var/reporting/runs/`, and exits when the strategy reaches a terminal phase.

This plan adds **`run_continue`**: a thin orchestration layer that repeatedly invokes the existing one-shot run path across consecutive **BTC Up/Down 5m** windows, without the operator supplying a new event URL each time.

**MVP scope (recommended):**

1. New CLI subcommand `run_continue` in `runtime/app.py`.
2. New module `runtime/run_continue.py` for session orchestration, wait scheduling, and graceful shutdown.
3. New module `ingestion/btc_5m_window_scheduler.py` (or extend `ingestion/market_discovery.py`) for reference-URL family validation and **next-window** selection with configurable pre-start lead (default 30s).
4. Refactor `cmd_run` body into a reusable `execute_run(...)` function (same behavior, no semantic change to `run`).
5. Loop: wait → resolve window metadata → call `execute_run` with derived `--event-url` and per-window `--run-name` → repeat until CTRL+C.
6. Session-level manifest + JSONL log under `var/reporting/run_continue/<session-name>/`.

**Deferred to Phase 2:**

- Optional `--record` integration (reuse `record_run` per window).
- Advanced CLI flags (`--continue-on-window-failure`, `--run-name-template`, `--window-family` override).
- Preflight hook at session start (reuse existing script).

**Deferred to hardening:**

- Windows signal-handler edge cases, clock skew monitoring, Gamma outage backoff policies, automated integration tests against live Gamma.

The design preserves the current trading loop, coordinator wiring, facts model, and one-shot `run` semantics. `run_continue` never rewrites `paired_binary_run.py` or survival enforcement.

---

## 2. Current Architecture Review

### 2.1 CLI entry and command routing

| Component | Location | Role |
|-----------|----------|------|
| CLI router | `src/tyrex_pm/runtime/app.py` → `main()` | Subcommands: `run`, `record`, `reset-state`, `live-attest` |
| One-shot run | `async def cmd_run(args)` in `app.py` | Full live/shadow bootstrap + strategy loop |
| Record-only | `async def cmd_record(args)` in `runtime/record_run.py` | WS event backbone recording, no trading |

`run` CLI flags today (`app.py` lines ~202–224):

- `--strategy`, `--scenario`, `--repo-root`, `--state-dir`, `--run-name`, `--event-url`
- Strategy-specific: `--once`, `--fixture`, `--max-iterations`

Dispatch: `asyncio.run(cmd_run(args))` → returns exit code written to `SystemExit`.

### 2.2 Config loading

`load_app_config()` in `runtime/config.py`:

1. Always loads `config/risk/default.yaml` + `config/runtime/default.yaml`.
2. Loads `--strategy` YAML.
3. Deep-merges `--scenario` overlay (bare name → `config/scenarios/<name>.yaml`).
4. Parses immutable `AppConfig`.

Scenario overlays can set `execution_mode: live`, `runtime.survival.*`, `runtime.market_data.*`, `runtime.paired_binary.*`, and `strategy.paired_binary.*` placeholders.

### 2.3 Event URL → paired-binary metadata

| Step | Module | Function |
|------|--------|----------|
| Parse URL/slug | `venue/polymarket/event_metadata.py` | `parse_event_ref(ref)` |
| Gamma fetch | same | `resolve_paired_binary_event_metadata(event_url)` |
| Merge into config | `runtime/paired_binary_metadata.py` | `resolve_and_apply_paired_binary_metadata(app, event_url=...)` |

**URL parsing behavior (already implemented and tested):**

- Full URLs: path segments after locale prefix (e.g. `/fr/event/...`) → slug is **last segment**.
- `test_parse_event_ref_accepts_polymarket_url` confirms `https://polymarket.com/fr/event/btc-updown-5m-1782938400` → slug `btc-updown-5m-1782938400`.
- Bare slug `btc-updown-5m-<unix_start>` also works.
- Slug timestamp regex: `_BTC_5M_SLUG_RE = r"btc-updown-5m-(\d{10,})$"`.
- Metadata derives `event_start_ts`, `event_end_ts` (default +300s), `market_id` via `btc_5m_market_id_from_end_ts()`, token IDs, `condition_id`.

**Important:** `cmd_run` treats `--event-url` as the **exact window to trade**. It does not skip in-progress windows. `run_continue` must add scheduling logic on top.

### 2.4 BTC 5m discovery (record path — reusable primitives)

`ingestion/market_discovery.py` already provides:

| Symbol | Purpose |
|--------|---------|
| `BTC_5M_WINDOW_S = 300` | Window length |
| `BTC_5M_SLUG_PREFIX = "btc-updown-5m-"` | Slug family |
| `is_btc_5m_event_slug(slug)` | Family check |
| `btc_5m_window_start_timestamps(now_ts, lookback, lookahead)` | Aligned 5m boundary timestamps |
| `discover_btc_5m_by_slug(slug)` | Gamma lookup → `MarketDiscoveryResult` |
| `classify_market_recording_eligibility(...)` | Recording-specific (`pre_open_recording_lead_s=60`) |
| `run_market_discovery(...)` | Continuous poll loop for recorder |

**Gap:** recording eligibility starts up to **60s before open** and may attach to the **current** window. Trading wants **next** window only, with **30s** pre-start default. Do **not** reuse `classify_market_recording_eligibility` for trading; add a dedicated scheduler.

### 2.5 One-shot `cmd_run` lifecycle (paired_binary live)

High-level flow in `cmd_run`:

1. `load_app_config` + optional `resolve_and_apply_paired_binary_metadata`.
2. Create `run_id` (UUID), `runs_dir = var/reporting/runs/<run-name or run_id>`, write `manifest.json`.
3. Instantiate strategy (`PairedBinaryStrategy`), `RuntimeCoordinator`, `LiveOMS` if live.
4. Start background tasks: heartbeat, venue refresh, user WS, market WS primary, REST bootstrap.
5. `recover_on_startup()` → `run_paired_binary_loop(...)`.
6. On terminal phase + `stop_background_tasks_after_strategy_done: true` (phase1 scenarios): cancel live tasks, exit loop.
7. `finally`: stop live tasks, stop OMS writer, write `run_summary.json`, return `exit_code`.

Per-window state:

- Paired-binary persistence: `var/state/paired_binary/<owner_id>/<market_id>.json` (unique per window).
- Allocation ledger: `var/state/allocation_ledger.json` (persists across windows — by design).
- Kill switches: `survival/kill_switch_runtime.py` with `persist_daily: true` in phase1 scenarios.

### 2.6 Record command (for Phase 2 reference)

`cmd_record` in `record_run.py`:

- Loads config via `load_app_config(strategy=paired_binary.yaml, scenario=--scenario)`.
- If `--event-url`: resolves metadata, runs `_run_single_market_record(...)`.
- If no `--event-url` and `recording.discovery_enabled: true`: `_run_discovery_record(...)` with `run_market_discovery`.
- Signal handling via `_register_signal_handlers(loop, stop)`.
- Output: `var/recordings/<YYYY-MM-DD>/<market_id>/` (+ external BTC/RTDS sidecars for rich scenarios).

Record uses its **own** lightweight coordinator (`SimpleNamespace` + `MarketStateStore`), separate from live trading WS — good for isolation, but doubles WS connections if run in parallel with live trading in the same process.

### 2.7 Existing tests to extend

| File | Coverage |
|------|----------|
| `tests/test_paired_binary_event_metadata.py` | URL parsing, Gamma resolution, merge |
| `tests/test_market_discovery.py` | Window alignment, slug discovery, discovery loop |
| `tests/test_record_resolve_tokens.py` | Record token resolution |
| `tests/test_phase1_live_scenario_preflight.py` | Live scenario validation |

No `run_continue` tests exist yet.

---

## 3. Target Behavior

### 3.1 Reference event URL

The user supplies **one** BTC 5m Polymarket URL (any age). The orchestrator:

1. Parses it with `parse_event_ref`.
2. Validates `is_btc_5m_event_slug(ref.slug)` — confirms **family** `btc-updown-5m`.
3. Does **not** trade that URL's window unless it happens to be the selected next window by clock logic.
4. Uses wall-clock 5m alignment for all subsequent window selection.

If the reference URL is not BTC 5m (e.g. `"toto"`), fail fast at session start with a clear error (same as today for invalid `--event-url`).

### 3.2 Next-window selection and pre-start timing

**Policy (MVP): always skip the in-progress window; trade the next full window.**

Example:

```text
now = 12:03:40 UTC
current window start = 12:00:00 (in progress → skip)
next window start     = 12:05:00
prestart_seconds      = 30
wake_at               = 12:04:30
action at wake_at     → start execute_run for slug btc-updown-5m-<12:05:00 unix>
```

Edge cases:

| Situation | Behavior |
|-----------|----------|
| `now < wake_at` | Sleep until `wake_at` (interruptible) |
| `now >= wake_at` and `now < next_window_start` | Start immediately (late pre-start still OK) |
| `now >= next_window_start` (missed window open) | Skip that window; compute following window; if still late, start immediately only if within strategy entry grace (document: prefer skip to avoid mid-window entry — see §5.3) |
| Old reference URL | Ignored for targeting; family validation only |

**Missed-window rule (MVP):** if `now >= next_window_start + entry_grace_s` (proposed default `15s`, configurable), skip to the **following** window rather than entering mid-window. This aligns with `block_new_entry_phases: [near_close, closed]` and the product goal of not starting mid-window.

### 3.3 Per-window execution

Each window invokes the **same** code path as `run` with:

- Same `--strategy` / `--scenario`
- Derived `--event-url` for that window's slug
- Derived `--run-name` (see §9)

### 3.4 Continuous operation

After `execute_run` returns:

1. Log window outcome to session log.
2. Increment window counter.
3. If `--max-windows` reached → exit cleanly.
4. Else compute next window and repeat wait → run cycle.

Stop only on:

- Operator CTRL+C / SIGTERM
- Unrecoverable session error (default)
- Optional `--continue-on-window-failure` (Phase 2)

### 3.5 Optional recording (Phase 2)

When `--record` is set, start a per-window record session aligned to the same resolved metadata, ideally **same process** via extracted record helper (see §8).

---

## 4. Proposed CLI

### 4.1 MVP command

```bash
python -m tyrex_pm.runtime.app run_continue \
  --strategy config/strategies/paired_binary.yaml \
  --scenario live_paired_binary_phase1_trailing_enforce \
  --run-name "phase1_trailing_continue" \
  --event-url "https://polymarket.com/fr/event/btc-updown-5m-1783369200" \
  --prestart-seconds 30
```

### 4.2 Full proposed surface

| Flag | Default | MVP? | Description |
|------|---------|------|-------------|
| `--strategy` | (required) | Yes | Same as `run` |
| `--scenario` | (required) | Yes | Same as `run`; bare name or path |
| `--run-name` | (required) | Yes | Session label (not per-window) |
| `--event-url` | (required) | Yes | Reference BTC 5m URL for family validation |
| `--repo-root` | auto | Yes | Same as `run` |
| `--state-dir` | `var/state` | Yes | Same as `run` |
| `--prestart-seconds` | `30` | Yes | Start `execute_run` at `window_start - N` |
| `--max-windows` | unlimited | Yes | Stop after N completed window attempts (testing) |
| `--sleep-granularity` | `1.0` | Yes | Interruptible wait poll interval (seconds) |
| `--entry-grace-seconds` | `15` | Yes | Max lateness after window open before skipping |
| `--dry-run` | off | Phase 2 | Log planned windows; no `execute_run` |
| `--shadow` | off | Phase 2 | Force `execution_mode=shadow` overlay for testing |
| `--continue-on-window-failure` | off | Phase 2 | Continue after non-zero window exit |
| `--run-name-template` | `{session}/{market_id}` | Phase 2 | Per-window run dir template |
| `--window-family` | inferred | Phase 2 | Override slug prefix (default from reference URL) |
| `--record` | off | Phase 2 | Enable per-window recording |
| `--record-scenario` | `record_btc5m_rich.yaml` | Phase 2 | Recording scenario overlay |
| `--record-lead-seconds` | `30` | Phase 2 | When to start recorder relative to window open |
| `--skip-current-window` | implicit true | MVP | Always skip in-progress window (no flag in MVP) |

### 4.3 MVP vs later

| Feature | MVP | Phase 2 | Hardening |
|---------|-----|---------|-----------|
| Next-window scheduler | ✓ | | |
| `execute_run` refactor | ✓ | | |
| Session manifest/log | ✓ | | |
| CTRL+C graceful shutdown | ✓ | | |
| `--max-windows` | ✓ | | |
| `--dry-run` | | ✓ | |
| `--continue-on-window-failure` | | ✓ | |
| `--record` | | ✓ | |
| `--run-name-template` | | ✓ | |
| Session preflight script hook | | ✓ | |
| Gamma backoff / retry policy | | | ✓ |
| Live integration smoke | | | ✓ |

---

## 5. Event Window Resolution Design

### 5.1 New module: `ingestion/btc_5m_window_scheduler.py`

**Rationale:** keep `market_discovery.py` focused on record discovery; trading scheduler has different eligibility rules.

Proposed public API:

```python
@dataclass(frozen=True)
class Btc5mWindowPlan:
    window_start_ts: int          # unix seconds, 300s-aligned
    window_end_ts: int            # start + 300
    event_slug: str               # btc-updown-5m-{start}
    event_url: str                # canonical https://polymarket.com/event/{slug}
    wake_at_ts: float             # window_start - prestart_seconds (clamped to now)
    skip_reason: str | None       # diagnostic when skipping a candidate

def validate_btc_5m_reference_url(reference_url: str) -> str:
    """Return slug; raise EventMetadataError if not btc-updown-5m family."""

def current_btc_5m_window_start(now_ts: float) -> int:
    """Floor now to 300s boundary."""

def select_next_btc_5m_trading_window(
    *,
    now_ts: float,
    prestart_seconds: float = 30.0,
    entry_grace_seconds: float = 15.0,
) -> Btc5mWindowPlan:
    """Return the next window to trade and when to wake."""

def following_btc_5m_window(plan: Btc5mWindowPlan) -> Btc5mWindowPlan:
    """After a completed run, advance by +300s."""
```

### 5.2 Algorithm: `select_next_btc_5m_trading_window`

```python
now = now_ts
aligned = int(now) - (int(now) % 300)

# Always skip in-progress window
candidate_start = aligned + 300

# If we're past open + entry_grace, skip this candidate
while now >= candidate_start + entry_grace_seconds:
    candidate_start += 300

wake_at = max(now, candidate_start - prestart_seconds)

return Btc5mWindowPlan(
    window_start_ts=candidate_start,
    window_end_ts=candidate_start + 300,
    event_slug=f"btc-updown-5m-{candidate_start}",
    event_url=f"https://polymarket.com/event/btc-updown-5m-{candidate_start}",
    wake_at_ts=wake_at,
)
```

**Answer to design Q1:** Resolve reference URL → validate family slug prefix; **do not** use reference timestamp as trade target. Select next aligned window from wall clock with skip-in-progress and lateness grace.

**Answer to design Q2:** Reuse `parse_event_ref`, `is_btc_5m_event_slug`, `discover_btc_5m_by_slug` / `resolve_paired_binary_event_metadata` for **per-window** metadata at run time. Add **new** scheduler functions above; do not reuse recording eligibility.

**Answer to design Q3:** `candidate_start = aligned + 300` always skips the current in-progress window. Additional `entry_grace_seconds` skip avoids mid-window starts if the orchestrator is late.

### 5.3 Gamma validation before each window

At `wake_at`, before `execute_run`:

1. Call `discover_btc_5m_by_slug(plan.event_slug)` (or `resolve_paired_binary_event_metadata(plan.event_url)`).
2. Verify `abs(meta.event_start_ts - plan.window_start_ts) <= 2` (tolerance for Gamma drift).
3. If 404 / not found: retry with backoff until `entry_grace_seconds` exhausted, then skip window and emit session error fact.

This mirrors record discovery's Gamma fetch but is invoked **once per scheduled window**, not continuous poll.

### 5.4 French / localized URLs

No change needed. Reference URL may be `/fr/event/...`; canonical generated URLs can omit locale (matches existing tests and Gamma slug API). Optionally preserve locale from reference in `event_url` field for operator familiarity (cosmetic).

---

## 6. Continuous Orchestration Design

### 6.1 New module: `runtime/run_continue.py`

Responsibilities:

| Function | Purpose |
|----------|---------|
| `async def cmd_run_continue(args) -> int` | CLI entry |
| `async def run_continue_session(config: RunContinueConfig) -> int` | Main loop |
| `async def wait_until(wake_at_ts, stop: asyncio.Event, granularity: float)` | Interruptible sleep |
| `def build_window_run_name(session_name, market_id, template?) -> str` | Per-window artifact naming |
| `def append_session_event(session_dir, event: dict)` | Session JSONL log |
| `def write_session_manifest(session_dir, manifest: dict)` | Session-level metadata |

### 6.2 Main loop (pseudocode)

```python
async def run_continue_session(cfg, stop: asyncio.Event) -> int:
    validate_btc_5m_reference_url(cfg.reference_event_url)
    init_session_artifacts(cfg.session_run_name)

    windows_attempted = 0
    while not stop.is_set():
        plan = select_next_btc_5m_trading_window(
            now_ts=time.time(),
            prestart_seconds=cfg.prestart_seconds,
            entry_grace_seconds=cfg.entry_grace_seconds,
        )
        append_session_event({"event": "window_scheduled", ...plan...})

        await wait_until(plan.wake_at_ts, stop, cfg.sleep_granularity)
        if stop.is_set():
            break

        # Optional Phase 2: start recording task here
        meta = await resolve_window_metadata(plan)  # Gamma
        run_name = build_window_run_name(cfg.session_run_name, meta.market_id)
        run_args = make_run_args(cfg, event_url=plan.event_url, run_name=run_name)

        append_session_event({"event": "window_run_start", "run_name": run_name})
        exit_code = await execute_run(run_args)
        append_session_event({"event": "window_run_complete", "exit_code": exit_code})

        windows_attempted += 1
        if cfg.max_windows and windows_attempted >= cfg.max_windows:
            break
        if exit_code != 0 and not cfg.continue_on_window_failure:
            return exit_code

        # Next iteration: select_next from now (following window logic)
    append_session_event({"event": "session_stopped", "reason": "operator_stop" if stop.is_set() else "completed"})
    return 0
```

After a successful window run, the next iteration calls `select_next_btc_5m_trading_window(now)` again (fresh clock), which naturally targets the next 5m boundary.

### 6.3 Signal handling

Mirror `record_run._register_signal_handlers`:

- Register SIGINT/SIGTERM → set `stop` Event.
- Top-level `cmd_run_continue` wraps session in `try/finally` to write session stop reason.

**Answer to design Q4:** `wait_until` uses monotonic/time.time comparison in a loop:

```python
while not stop.is_set():
    remaining = wake_at_ts - time.time()
    if remaining <= 0:
        return
    await asyncio.wait_for(stop.wait(), timeout=min(remaining, granularity))
```

Use `asyncio.wait_for(stop.wait(), timeout=...)` pattern from `run_market_discovery` (proven in repo).

---

## 7. Integration With Existing run Command

### 7.1 Preferred approach: extract `execute_run`

**Do not** subprocess `python -m tyrex_pm.runtime.app run` (breaks Windows quoting, duplicates `.env` loading, harder CTRL+C propagation).

**Do** extract the body of `cmd_run` into:

```python
# runtime/run_once.py  (or bottom of app.py initially)
async def execute_run(args: argparse.Namespace) -> int:
    ...  # current cmd_run body unchanged ...
```

Then:

```python
async def cmd_run(args: argparse.Namespace) -> int:
    return await execute_run(args)
```

`run_continue` imports `execute_run` and constructs an `argparse.Namespace` (or small `@dataclass RunOnceParams` converted to Namespace) with:

- `strategy`, `scenario`, `repo_root`, `state_dir` from session config
- `run_name` = per-window name
- `event_url` = resolved window URL
- `once=False`, `fixture=None`, `max_iterations=None`

### 7.2 Why this is safe

- Each `execute_run` call creates a **new** `run_id`, `runs_dir`, coordinator, OMS writer, and live task group.
- Paired-binary `run_paired_binary_loop` already exits on terminal phase; phase1 scenarios set `stop_background_tasks_after_strategy_done: true`.
- `cmd_run`'s `finally` block stops live tasks and OMS — no leak between windows if `execute_run` returns normally.
- **No change** to strategy logic, survival, or pipeline.

### 7.3 Answer to design Q6

Pass resolved window URL via `args.event_url` into `execute_run` → existing `resolve_and_apply_paired_binary_metadata(app, event_url=...)` fills paired-binary placeholders exactly as today.

### 7.4 Strategy kind restriction (MVP)

MVP supports **`paired_binary` only** (matches product request). If `strategy_kind != paired_binary`, fail at session start with clear error. Other kinds can be added later once window scheduling is generalized.

### 7.5 Live prerequisites

Same as `run`:

- `TYREX_PRIVATE_KEY`, `tyrex-pm[live]`
- Scenario `execution_mode: live`
- Recommend documenting session-start check: operator runs `scripts/preflight_phase1_live_scenario.py` manually before `run_continue` (automate in Phase 2).

---

## 8. Optional Recording Mode

### 8.1 Recommendation: Phase 2, same-process helper

**Avoid MVP recording** — dual WS stacks (live trading + rich recorder with Binance/RTDS) increase latency and connection count risk.

When implemented:

1. Extract from `record_run.py`:

```python
async def run_window_record_session(
    *,
    app: AppConfig,
    rec,
    repo_root: Path,
    scenario: str,
    event_meta: PairedBinaryEventMetadata,
    stop: asyncio.Event,
) -> RecordSessionState:
    """Thin wrapper around _run_single_market_record + external feeds."""
```

2. `run_continue` starts `asyncio.create_task(run_window_record_session(...))` at `wake_at_ts` (or `window_start - record_lead_seconds`).
3. Await record task completion after `execute_run` returns (or cancel on window failure / operator stop).
4. Use **same** `event_meta` resolved for trading to guarantee token/window alignment.

### 8.2 Subprocess alternative (not preferred)

```bash
python -m tyrex_pm.runtime.app record --scenario ... --event-url <window_url>
```

Pros: isolation. Cons: duplicate external feeds, harder synchronized stop, parent must track PID. Use only if same-process WS contention is observed in testing.

### 8.3 Per-window vs continuous recording

**Per-window** (recommended): one record session per trading window, stopped after `event_end_ts + post_close_grace_s`. Matches existing `_market_lifecycle_loop` in `record_run.py`.

Do **not** use `recording.discovery_enabled` continuous discovery alongside `run_continue` — two schedulers would diverge.

### 8.4 Artifact layout

Trading: `var/reporting/runs/<window-run-name>/facts.jsonl`

Recording: `var/recordings/<YYYY-MM-DD>/<market_id>/` (unchanged)

Session log links both paths per window.

### 8.5 Latency mitigation

- Recorder uses separate `MarketStateStore` today (no shared coordinator) — acceptable.
- For rich scenario, external BTC/RTDS are process-global; starting once per session (not per window) is a Phase 2 optimization.
- MVP: no recording → no contention.

**Answer to design Q9 (MVP):** defer recording; document Phase 2 same-process helper above.

---

## 9. Reporting and Artifacts

### 9.1 Session-level artifacts

Directory: `var/reporting/run_continue/<session_run_name>/`

| File | Content |
|------|---------|
| `session_manifest.json` | `session_id`, `git_sha`, strategy/scenario paths, reference URL, `prestart_seconds`, started/stopped timestamps |
| `session_log.jsonl` | `window_scheduled`, `window_run_start`, `window_run_complete`, `window_skipped`, `session_stopped` |

### 9.2 Per-window artifacts (unchanged)

`var/reporting/runs/<window-run-name>/`

- `manifest.json`, `facts.jsonl`, `run_summary.json`

### 9.3 Run naming

**MVP default:**

```text
{session_run_name}__{market_id}
```

Example: `phase1_trailing_continue__btc_5m_20260705_1705`

Implementation: reuse `_safe_run_dir_label()` from `app.py` on the composed string.

**Phase 2 template** (`--run-name-template`):

Supported placeholders: `{session}`, `{market_id}`, `{window_start_ts}`, `{event_slug}`, `{window_end_hhmm}`

### 9.4 Answer to design Q5

Unique folders via `{session_run_name}__{market_id}` where `market_id` comes from resolved Gamma metadata (`btc_5m_YYYYMMDD_HHMM` from end timestamp). Each window has distinct `market_id` → distinct run dir. UUID `run_id` inside facts remains unique per window.

---

## 10. Failure Handling and Shutdown Semantics

### 10.1 CTRL+C / SIGTERM

| Phase | Behavior |
|-------|----------|
| Waiting for next window | Set `stop` Event → exit loop → write `session_stopped` reason `operator_stop` → return 0 |
| `execute_run` active | Propagate stop: `run_continue` cannot easily interrupt inner loop without refactoring `execute_run` to accept `stop` Event (**Phase 2**). **MVP:** first CTRL+C sets stop flag; if inside `execute_run`, wait for natural terminal exit OR document that operator may need second CTRL+C / kill process. **Hardening:** pass optional `stop: asyncio.Event` into `execute_run` → `run_paired_binary_loop(stop=...)`. |
| Recording task (Phase 2) | Cancel record task; await cleanup in `finally` |

**MVP pragmatic approach:** register signal handler that sets session `stop`. If `execute_run` is running, log note that shutdown will complete after current window. Optional fast-abort in hardening via shared stop Event wired to `stop_live` in `cmd_run`.

### 10.2 Window failures

| Failure | MVP behavior |
|---------|--------------|
| Gamma metadata 404 at schedule time | Skip window, log `window_skipped`, continue |
| `execute_run` returns non-zero | Log `window_run_failed`, **stop session** (safe default) |
| `execute_run` returns 2 (metadata error) | Same |
| Kill switch hard stop mid-window | Terminal phase → normal exit; session continues to next window unless kill switch persists blocking (document: daily kill switch may block subsequent entries — expected safety behavior) |

**Answer to design Q8:** MVP stops session on first window failure (`exit_code != 0`). Phase 2 adds `--continue-on-window-failure`.

### 10.3 State persistence between windows

| State file | Cross-window behavior |
|------------|----------------------|
| `paired_binary/<owner>/<market_id>.json` | Isolated per window |
| `allocation_ledger.json` | Persists — token IDs differ per window; verify no stale reservations (existing runtime clears on run boundary via fresh coordinator) |
| Kill switch counters | May persist — intentional safety |

No automatic `reset-state` between windows in MVP. Document that operators should monitor kill switch facts across session.

---

## 11. Testing Plan

### 11.1 Unit tests — `tests/test_btc_5m_window_scheduler.py` (new)

| Test | Asserts |
|------|---------|
| `test_validate_reference_url_rejects_non_btc_slug` | `"toto"` raises |
| `test_validate_reference_accepts_fr_polymarket_url` | FR URL passes family check |
| `test_select_next_window_skips_in_progress` | `now=12:03:40` → next start `12:05:00`, wake `12:04:30` |
| `test_select_next_window_starts_immediately_when_past_wake` | `now=12:04:45` → wake clamped to now |
| `test_select_next_window_skips_when_past_entry_grace` | `now=12:05:20` → target `12:10:00` |
| `test_following_window_advances_by_300s` | Sequential plans |
| `test_old_reference_url_does_not_select_past_window` | Reference ts far in past; plan based on now |

Use injected `now_ts` — no network.

### 11.2 Unit tests — `tests/test_run_continue_orchestrator.py` (new)

| Test | Asserts |
|------|---------|
| `test_run_name_generation` | Session + market_id → safe folder name |
| `test_wait_until_respects_stop_event` | Stop during wait exits promptly |
| `test_session_log_writes_events` | JSONL sequence |
| `test_max_windows_stops_session` | Loop terminates after N |
| `test_window_failure_stops_session_by_default` | Non-zero exit_code aborts |
| `test_continue_on_failure_flag` | Phase 2 flag continues loop |

Mock `execute_run` with `AsyncMock` returning 0/1.

### 11.3 Integration tests — `tests/test_run_continue_integration.py` (new)

| Test | Asserts |
|------|---------|
| `test_cmd_run_continue_dry_run_phase2` | Schedules without execute (Phase 2) |
| `test_execute_run_still_one_shot` | Regression: existing `run` unchanged |

### 11.4 Regression tests (existing)

- Run full `tests/test_paired_binary_event_metadata.py`
- Run full `tests/test_market_discovery.py`

### 11.5 Manual live protocol

Before production session:

1. `preflight_phase1_live_scenario.py` with reference URL.
2. `run_continue --max-windows 2` on live tiny scenario.
3. Verify two distinct run dirs, session log, clean CTRL+C between windows.

**Answer to design Q10:** unit tests for scheduler + orchestrator with mocked `execute_run`; no live Gamma in CI; manual two-window live smoke before relying on continuous trading.

---

## 12. Phased Implementation Plan

### Phase 0 — Refactor (no behavior change)

1. Extract `execute_run` from `cmd_run` in `runtime/run_once.py`.
2. `cmd_run` delegates to `execute_run`.
3. Add regression test that `cmd_run` import path unchanged.

**Files touched:** `runtime/app.py`, new `runtime/run_once.py`, `tests/test_run_once_smoke.py` (optional thin test).

### Phase 1 — MVP `run_continue`

1. Add `ingestion/btc_5m_window_scheduler.py` with functions in §5.1.
2. Add `runtime/run_continue.py` with session loop §6.
3. Wire CLI in `app.py`: subparser `run_continue`, dispatch `asyncio.run(cmd_run_continue(args))`.
4. Session artifacts §9.1.
5. Unit tests §11.1–11.2.
6. Docs: short section in `Docs/OPERATIONS.md` (operator usage) — separate doc PR acceptable.

**Estimated new code:** ~350–450 lines excluding tests.

### Phase 2 — Hardening + optional features

1. Pass `stop: asyncio.Event` into `execute_run` for mid-window CTRL+C.
2. `--continue-on-window-failure`, `--dry-run`, `--run-name-template`.
3. Extract `run_window_record_session`; implement `--record` / `--record-scenario`.
4. Auto-run preflight at session start (optional flag).
5. Gamma retry/backoff before window skip.

### Phase 3 — Later improvements

- Generalize to other rolling event families (not only BTC 5m).
- Metrics dashboard from `session_log.jsonl`.
- Coordinator reuse across windows (performance; high risk — defer indefinitely).

---

## 13. Acceptance Criteria

### MVP done when:

- [ ] `python -m tyrex_pm.runtime.app run_continue --help` documents all MVP flags.
- [ ] Reference FR URL validates; invalid slug fails at start with exit code 2.
- [ ] Orchestrator waits until `window_start - prestart_seconds` (± sleep granularity).
- [ ] Never targets the currently in-progress 5m window when started mid-window.
- [ ] Each window produces a normal `var/reporting/runs/<name>/` artifact tree via `execute_run`.
- [ ] Session log records schedule/start/complete for each window.
- [ ] CTRL+C during wait stops before next window.
- [ ] `--max-windows 1` runs exactly one window and exits.
- [ ] Existing `run` command behavior unchanged (CI green).
- [ ] All new unit tests pass without network.

### Phase 2 done when:

- [ ] `--record` produces aligned `var/recordings/<day>/<market_id>/` for each traded window.
- [ ] `--continue-on-window-failure` documented and tested.
- [ ] Mid-window CTRL+C propagates to strategy loop within bounded time.

---

## 14. Open Questions / Risks

| Item | Risk | Mitigation |
|------|------|------------|
| Kill switch persists across windows | Later windows blocked after bad streak | Document; operator monitors session log; optional `--reset-kill-switch` deferred |
| Allocation ledger cross-window | Stale reservation edge case | Fresh coordinator per `execute_run`; add test with two mocked windows |
| Gamma slug not published until near open | Window skip | Retry until entry grace; log `window_skipped` |
| Clock skew vs Polymarket | Wrong window selection | Compare Gamma `event_start_ts` to plan; tolerance ±2s |
| Mid-window CTRL+C in MVP | Must wait for terminal phase | Document; Phase 2 shared stop Event |
| Dual WS with recording | Latency | Phase 2 only; prefer per-window isolated record store |
| `allow_terminal_state_resume: false` | Correct for fresh window | Different `market_id` per window → new state file automatically |
| Operator supplies current-window URL expecting immediate trade | Confusion | CLI help: reference URL is family only; first trade is **next** window |
| Windows signal handlers | `add_signal_handler` limited on Windows | Same pattern as `record_run`; document second CTRL+C |
| Subprocess vs in-process | Operational complexity | MVP in-process `execute_run` only |

### Explicit answers to design questions (index)

1. **Old URL → next window:** family validate reference; clock-based `select_next_btc_5m_trading_window`.
2. **Reuse:** `parse_event_ref`, `is_btc_5m_event_slug`, `resolve_paired_binary_event_metadata`, `discover_btc_5m_by_slug`; **add** scheduler module.
3. **Avoid mid-window:** skip `aligned` current window; `entry_grace_seconds` skip if late.
4. **Wait reliably:** interruptible `wait_until` with `sleep_granularity` (default 1s).
5. **Unique run folders:** `{session}__{market_id}`.
6. **Pass metadata:** per-window `event_url` into `execute_run` → existing resolver.
7. **CTRL+C:** session stop Event; MVP completes active window; hardening passes stop into run loop.
8. **Window failure:** MVP abort session; Phase 2 optional continue.
9. **Recording:** Phase 2; same-process `run_window_record_session` per window.
10. **Tests:** §11 — scheduler unit tests + orchestrator mocks + manual two-window live smoke.

---

## Appendix A — File change checklist

| Action | Path |
|--------|------|
| Create | `src/tyrex_pm/ingestion/btc_5m_window_scheduler.py` |
| Create | `src/tyrex_pm/runtime/run_continue.py` |
| Create | `src/tyrex_pm/runtime/run_once.py` (extract from app.py) |
| Modify | `src/tyrex_pm/runtime/app.py` — subparser + dispatch; thin `cmd_run` |
| Create | `tests/test_btc_5m_window_scheduler.py` |
| Create | `tests/test_run_continue_orchestrator.py` |
| Update | `Docs/OPERATIONS.md` — operator section (post-MVP) |

## Appendix B — Example session log lines

```json
{"event":"session_started","session_run_name":"phase1_trailing_continue","reference_event_url":"https://polymarket.com/fr/event/btc-updown-5m-1783369200","prestart_seconds":30}
{"event":"window_scheduled","window_start_ts":1783370100,"wake_at_ts":1783370070.0,"event_slug":"btc-updown-5m-1783370100"}
{"event":"window_run_start","run_name":"phase1_trailing_continue__btc_5m_20260705_1715","event_url":"https://polymarket.com/event/btc-updown-5m-1783370100"}
{"event":"window_run_complete","run_name":"phase1_trailing_continue__btc_5m_20260705_1715","exit_code":0}
{"event":"session_stopped","reason":"operator_stop"}
```
