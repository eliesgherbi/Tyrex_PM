# Phase 9 — CURRENT STATUS

**As of:** 2026-08-07 (remediation honesty overlay)  
**Overall Phase 9:** **BLOCKED**

> This document is the **current** status overlay. Historical Phase 9 reports
> (`phase_9_acceptance_report.md`, `phase_09_completion_report.md`, and related
> evidence under this directory) remain historical evidence and are **not**
> rewritten. Do **not** treat phrases such as `PASS with blocker` or
> `COMPLETE_WITH_EXTERNAL_VALIDATION_BLOCKER` as if Phase 9 passed.

## Level summary (current)

| Level | Result | Evidence pointers |
|---:|---|---|
| 1 Unit/contract matrix (30 scenarios) | **PASS** | `phase_9_acceptance_report.md` Level 1; `tests/test_n7_lifecycle_acceptance.py` |
| 2 FakeTransport integrated lifecycle → FLAT_CONFIRMED | **PASS** | `fake_lifecycle_run/result.json` |
| 3 Incident replay (false-flat vs non-PASS) | **PASS** | `incident_replay_result.json` |
| 4 OBSERVE/SHADOW regression | **PASS** | `observe_shadow_regression/` |
| 5 Two-rollover operational market-data | **BLOCKED** | Duration / not executed — `two_rollover_market_data/blocker_note.json` |
| 6 Authenticated read-only preflight | **PASS** | `authenticated_readonly_preflight/` (`go_no_go=GO`, mutations=0) |

## Blocker (Level 5)

Level 5 requires ≥2 consecutive BTC 5-minute rollovers (~10+ minutes) of
**mutation-disabled** OBSERVE then SHADOW on an operator host. That long-lived
session was **not** executed in the Phase 9 agent run. Public venue read probes
succeeded; live admission remains disabled.

Exact blocker code: `duration_gate_not_run_in_phase9_agent`  
See: `two_rollover_market_data/blocker_note.json`

## Operator commands (corrected)

**Continuous SHADOW on production BTC 5m data (mutations disabled):**

```text
python -m tyrex_pm.application.cli run \
  --mode shadow \
  --strategy config/strategies/z_gap.yaml \
  --risk config/risk/tiny_live_5usd.yaml \
  --execution config/execution/shadow_live_5usd.yaml \
  --runtime config/runtime/shadow_btc_5m_continuous.yaml \
  --run-name shadow_validation
```

Validate config only (offline):

```text
python -m tyrex_pm.application.cli run \
  --mode shadow \
  --strategy config/strategies/z_gap.yaml \
  --risk config/risk/tiny_live_5usd.yaml \
  --execution config/execution/shadow_live_5usd.yaml \
  --runtime config/runtime/shadow_btc_5m_continuous.yaml \
  --run-name shadow_validation \
  --validate-config
```

Do **not** pass `--live`. Do **not** arm mutations. Capture ≥2 consecutive BTC 5m
rollovers (`min_completed_rollovers: 2`), then export evidence into
`two_rollover_market_data/`. Level 5 remains **BLOCKED** until that operator run
produces a PASS report — this overlay does not claim operational PASS.

Also see: `remediation_evidence/corrected_l5_commands.txt`,
`remediation_evidence/phase9_status.json`,
`SHADOW_READINESS_IMPLEMENTATION_REPORT.md`.

## Admission

`tiny_live_admitted` remains **false**. Phase 10 tiny-live must not be authorized
until Level 5 is PASS and the admission checklist is fully YES.
