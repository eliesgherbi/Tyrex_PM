# M8 WS-primary validation plan

**Status:** **`M8_VALIDATED`** on `m8_validation_002c` — see [m8_validation_report.md](m8_validation_report.md) E4 section.

## Preconditions

- Scenario: `config/scenarios/m8_ws_primary_validation_002.yaml` (balanced market overlay)
- Preflight: `python scripts/m8_preflight_check.py --scenario m8_ws_primary_validation_002`
- Market scan: `python scripts/m8_market_preflight.py --json-out var/m8_market_candidate_002.json`
- Rollback scenario tested in CI via `tests/test_m8_rollback_drill.py`, `tests/test_cutover_acceptance_criteria.py`, `tests/test_ws_primary_cutover.py`
- Group C observability enabled (`emit_decision_snapshot: true`)

## Required live/staging checks

| # | Check | Pass criteria |
|---|--------|----------------|
| 1 | Duration | WS-primary dry/live run **≥30 minutes** OR one **complete paired-binary lifecycle**, whichever is longer |
| 2 | p95 book age | **Strict** decisions (ENTRY/TP/ACTIVATION): p95 `book_age_ms` **< 750ms**; report all-decision p95 separately |
| 3 | p99 book age | **Strict** decisions: p99 **< 1500ms**; report all-decision p99 separately |
| 4 | REST entry block | **Zero** new paired-binary **OMS/submit** from `REST_BOOTSTRAP`, `REST_POLL`, or `REST_RECOVERY` (bootstrap observability snapshots excluded) |
| 5 | Decision facts | Every entry/stop/TP has `decision_id`, `snapshot_id`, `source`, `book_age_ms`, quality verdict |
| 6 | Planner evidence | Every FAK plan has `ExecutableBookView` and `PlannerEvidence` |
| 7 | REST poll off | `rest_poll_disabled` fact or log proof; no steady-state REST poll task |
| 8 | WS disconnect | New entries blocked; `market_readiness_transition` → `paused` |
| 9 | Reconnect | REST resync + fresh WS both legs + quality PASS before `trading_enabled` |
| 10 | Rollback | Config rollback to shadow+REST restores M1 behavior without code change |
| 11 | Scheduler | Mix of `paired_binary_tick_source: event_wake` and `timer` in facts (timer may be sparse under heavy WS) |
| 12 | Latency chain | Real ack/fill timing on matched OMS; `submit_to_ack_ms` + `trigger_to_fill_ms` non-null (or explicit unavailable reason) |

## Suggested commands

```bash
# Step 1 — preflight (config, hashes, position_size, wallet)
python scripts/m8_preflight_check.py

# Step 2 — staging / live WS-primary run (≥30 min or lifecycle)
python -m tyrex_pm.runtime.app run \
  --strategy config/strategies/paired_binary.yaml \
  --scenario m8_ws_primary_validation \
  --run-name m8_validation_002c

# Step 3–4 — analyze facts + acceptance table (split strict/risk book-age)
python scripts/validate_m8_ws_primary_run.py var/reporting/runs/m8_validation_002c \
  --json-out var/reporting/runs/m8_validation_002b/m8_validation_summary.json \
  --md-out var/reporting/runs/m8_validation_002b/m8_validation_summary.md
```

**Market selection note:** both legs must satisfy `min_usd` at `position_size: 5` (each leg ask × 5 ≥ $1). Override tokens in scenario only — do not change strategy params.

## Rollback procedure

Set in scenario or runtime overlay:

```yaml
runtime:
  market_data:
    websocket:
      primary_enabled: false
      shadow_enabled: true
    rest:
      poll_enabled: true
    quality:
      enforcement_mode: observe_only
```

Restart process. Confirm WS no longer writes authoritative store and REST poll resumes. Verified in `tests/test_m8_rollback_drill.py`.

## Sign-off blockers

- **`M8_VALIDATED`** on E4 run `m8_validation_002c` — strict book-age + latency + lifecycle
- Criteria **8–9** accepted via simulated reconnect drill if live drill unsafe
- M9 baseline artifacts still missing (blocks M9, not M8)
