# Milestone 8 — WS-Primary Cutover (Required Production Transition)

## Objective

Make **WebSocket the authoritative live book source** for all strategy/risk/execution decisions. Demote REST to bootstrap, reconnect resync, reconciliation, and diagnostics. **This milestone is required — shadow mode (M1) is not the production end state.**

## Why this milestone exists

M1–M7 build shadow validation, store v2, quality gate, planner, features, scheduler, and facts. M8 is the **mandatory production transition** where WS writes to the **authoritative** `MarketStateStore` and REST poll is disabled in healthy steady state.

## Current codebase state

**Implemented (Group D):** `primary_enabled` config; WS ingest writes authoritative store; readiness tracker + facts wired in `app.py`; REST poll gated; entry/TP/stop gates in `decision_gate.py`, `paired_binary_run.py`, `monitor.py`; rollback via config.

**Default runtime (no scenario overlay):** REST authoritative, WS shadow-only, `observe_only` — unchanged from pre-M8.

**Production sign-off:** Requires live/staging validation per [m8_validation_plan.md](m8_validation_plan.md).

## Target behavior

```text
WS healthy:
  apply to authoritative MarketStateStore (source=WEBSOCKET, source_quality=WS_PRIMARY)
  REST poll loop DISABLED
  MarketStateStoreShadow retired or unused

REST_BOOTSTRAP:  startup only — not sufficient for TRADING_ENABLED
REST_RECOVERY:   after reconnect/gap — not sufficient for new entries
REST_POLL:       deprecated; disabled when ws_primary_enabled && WS connected

New paired-binary entry:  WS_PRIMARY + DataQualityGate PASS
Take profit:              WS_PRIMARY + DataQualityGate PASS
Stop / urgent exit:       PASS | DEGRADED | EMERGENCY_ONLY per M3 rules
REST recovery:            risk reduction only if allow_rest_recovery_for_exit
                            never create or increase exposure
```

### Cutover from M1 shadow architecture

```text
Before M8: WS → MarketStateStoreShadow; REST → MarketStateStore (authoritative)
After M8:  WS → MarketStateStore (authoritative); REST bootstrap/recovery only
           No paired-binary code reads shadow store
```

### Reconnect (production)

```text
WS disconnect:
  block entries; PAUSED/DEGRADED
  emergency exits per M3; optional REST_RECOVERY refresh

WS reconnect:
  REST_RECOVERY bootstrap both legs
  wait fresh WS book both legs
  clear reconnect_gap only after DataQualityGate PASS
  restore TRADING_ENABLED
```

### Event ordering

Same deterministic rules as [M1](milestone_1_ws_shadow_ingestion.md) — now on **authoritative** store.

### Rollback

```yaml
runtime:
  market_data:
    websocket:
      primary_enabled: false
      shadow_enabled: true   # optional fallback to M1 behavior
    rest:
      poll_enabled: true
```

Rollback procedure documented and tested via config.

## Files likely touched

- `src/tyrex_pm/runtime/market_data_runtime.py` — WS → authoritative; disable poll
- `src/tyrex_pm/runtime/app.py` — primary WS task, not shadow
- `src/tyrex_pm/ingestion/market_stream.py` — write authoritative store
- `src/tyrex_pm/runtime/config.py`
- `src/tyrex_pm/market_data/quality.py` — WS_PRIMARY enforcement
- `config/scenarios/live_paired_binary_tiny.yaml`

## New files likely created

- `tests/test_ws_primary_cutover.py`
- `tests/test_rest_fallback_policy.py`
- `tests/test_cutover_acceptance_criteria.py`
- `config/scenarios/live_paired_binary_ws_primary.yaml`

## Config changes

```yaml
runtime:
  market_data:
    websocket:
      primary_enabled: true
      shadow_enabled: false
    rest:
      poll_enabled: false
      bootstrap_on_startup: true
      recovery_on_reconnect: true
    quality:
      require_ws_primary_for_entry: true
      allow_rest_recovery_for_exit: true
      allow_rest_recovery_for_entry: false
```

**Deployment choice:** `allow_rest_recovery_for_exit: false` for first production run is allowed — document in scenario YAML.

## Facts / observability changes

- `market_data_source_primary` — `ws`
- `rest_poll_disabled`
- `ws_primary_cutover`
- `ws_vs_rest_book_compare` — optional health sanity after REST bootstrap

## Tests to add or update

- WS connected → poll task not scheduled
- Entry blocked on REST_* sources when WS required
- Exit with REST_RECOVERY when configured; never increases exposure
- Reconnect sequence restores TRADING_ENABLED
- Rollback config restores poll + blocks WS primary writes
- All acceptance tests below

## Acceptance criteria (strengthened — all required)

Automated in CI: REST entry block, poll gating, rollback config, reconnect gap, readiness transitions.

Live/staging (see [m8_validation_plan.md](m8_validation_plan.md)):

1. [ ] WS-primary dry/live run **≥30 minutes** OR one **complete paired-binary lifecycle**, whichever is longer
2. [ ] p95 decision `book_age_ms` **< 750ms**
3. [ ] p99 decision `book_age_ms` **< 1500ms**
4. [ ] **Zero** new paired-binary entries from `REST_BOOTSTRAP`, `REST_POLL`, or `REST_RECOVERY`
5. [ ] Every entry/stop/TP decision has `decision_id`, `snapshot_id`, `source`, `book_age_ms`, quality verdict
6. [ ] Every FAK plan has `ExecutableBookView` and `PlannerEvidence`
7. [x] REST poll loop **disabled** in healthy WS-primary config (unit tests + `rest_poll_disabled` fact)
8. [x] WS disconnect **blocks** new entries (readiness tracker tests)
9. [x] Reconnect requires REST resync + fresh WS book both legs before `TRADING_ENABLED` (unit tests)
10. [x] Rollback documented and **tested through config** (unit tests)
11. [ ] Mix of `event_wake` / `timer` on live run
12. [ ] `latency_chain` with real ack/fill/user-WS timing on live stop exit

## Risks

- WS instability — rollback is one config change
- Missed deltas — reconnect_gap + REST_RECOVERY until PASS

## Open questions

- Low-frequency REST sanity poll (e.g. 60s) — **default: no poll in healthy state**; optional diagnostic mode only

## Definition of done

All 10 acceptance criteria met on live/staging run; REST policy enforced; rollback tested; team sign-off for M9 experiment.

## Not in scope

- M9 before/after report
- Strategy changes
- FeatureBuilder v1
