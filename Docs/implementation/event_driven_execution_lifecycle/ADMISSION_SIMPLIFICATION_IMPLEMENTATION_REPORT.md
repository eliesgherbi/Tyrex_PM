# Admission Simplification Implementation Report

**Outcome: COMPLETE**

**Date:** 2026-08-08  
**Scope:** Remove the external admission-artifact / operator-supplied configuration-fingerprint ceremony from the real N7 tiny-LIVE execution path. Keep fail-closed safety controls and internal mutation-transport arming.

**Real venue mutations caused by this task: 0**

---

## 1. Overall outcome

**COMPLETE**

The canonical operator authorization for bounded real-LIVE is now:

- `--mode live` selects the live runtime
- `--live` explicitly enables the bounded mutation path after preflight and remaining safety checks
- without `--live`, the command is read-only (zero mutations)
- no admission JSON, artifact ID, reviewed fingerprint, or `tiny_live_admitted` value is required

---

## 2. Behavior before vs after

### Before

1. Operator created/reviewed an admission JSON artifact.
2. Operator computed / copied a reviewed configuration fingerprint.
3. Operator set `tiny_live_admitted=true` in the artifact.
4. Operator passed `--admission-artifact <path>`.
5. Host validated fingerprint match, single-use consumption, and sealed admission fields.
6. Only then could `arm_operator_live()` enable the mutation transport.

### After

1. Operator runs the canonical YAML live command with `--live`.
2. Preflight and remaining production safety gates run as before.
3. `arm_operator_live()` fail-closes on force-off / CI / pytest / `TYREX_N7_FORBID_LIVE` / FakeTransport / user-stream unreadiness.
4. On success it creates an internal `MutationArmToken`, enables the transport network, enables bounded live/OMS mutation flags, enforces FAK, and records `authorization_source: "cli_--live"`.
5. Reports distinguish `live_requested` from `operator_live_armed`.

Internal automatic sealed fingerprints (`N7SealedConfig.fingerprint()`) remain for reports/persistence/diagnostics only. Operators do not create or supply them.

---

## 3. Files added, changed, and deleted

### Added

| Path | Role |
|---|---|
| `src/tyrex_pm/runtime/n7_live_safety.py` | Env forbid helper (`forbid_live_env_active`) |
| `tests/test_admission_simplification.py` | Focused replacement coverage for CLI/--live arming |
| `Docs/implementation/event_driven_execution_lifecycle/ADMISSION_SIMPLIFICATION_IMPLEMENTATION_REPORT.md` | This report |

### Changed (this task)

| Path | Role |
|---|---|
| `src/tyrex_pm/application/cli.py` | Removed `--admission-artifact`; help + `live_requested` / `operator_live_armed` reporting |
| `src/tyrex_pm/runtime/n7_oneshot_host.py` | Simplified `arm_operator_live`; removed admission state/consumption |
| `src/tyrex_pm/runtime/n7_sealed.py` | Removed `tiny_live_admitted` / `lifecycle_safety_invariants_present` |
| `src/tyrex_pm/runtime/n7_live_session.py` | Dropped artifact plumbing; reporting fields updated |
| `src/tyrex_pm/runtime/n7_operator_run.py` | Dropped artifact parameter; dry-run reporting fields |
| `src/tyrex_pm/runtime/yaml_config/live_run.py` | Dropped artifact parameter; reporting fields |
| `src/tyrex_pm/runtime/n7_dress_rehearsal.py` | Generic mutation-disabled rehearsal (no admission false gate) |
| `src/tyrex_pm/runtime/n7_operator_monitor.py` | Replaced admission snapshot with operator-arm fields |
| `src/tyrex_pm/runtime/yaml_config/resolve.py` | Comment cleanup |
| `src/tyrex_pm/runtime/n7_timing.py` | Comment cleanup |
| `config/execution/polymarket_live.yaml` | Comment cleanup (defaults still mutations OFF) |
| `tools/n7_live/run_n7_live_oneshot.py` | Removed `--admission-artifact`; reporting |
| `tools/n7_live/run_n7_dress_rehearsal.py` | Removed admission args |
| `tools/n7_live/README.md` | Canonical `--live` docs |
| `Docs/.../operator_tiny_live_runbook.md` | Current operator instruction (supersedes artifact workflow) |
| `Docs/.../monitoring_fields.md` | Operator-arm fields |
| `Docs/.../tiny_live_admission_checklist.md` | Marked historical / superseded |
| `Docs/.../admission_artifact_template.json` | Marked historical only |
| `tests/test_n7_dress_rehearsal.py` | Rewritten |
| `tests/test_n7_phase10_readiness.py` | Rewritten without admission ceremony |
| `tests/test_n7_terminal_safety.py` | Arming gates without admission state |
| `tests/test_n7_remediation_production_path.py` | Spy-based `--live` arming tests |
| `tests/test_n7_lifecycle_acceptance.py` | Removed `tiny_live_admitted` assertions |

### Deleted

| Path | Reason |
|---|---|
| `src/tyrex_pm/runtime/n7_admission.py` | External admission ceremony module |
| `src/tyrex_pm/runtime/n7_config_fingerprint.py` | Operator-reviewed fingerprint tooling |
| `tools/n7_live/write_n7_config_fingerprint.py` | Operator fingerprint CLI |

---

## 4. Complete call-chain removal summary

Removed `admission_artifact_path` / `--admission-artifact` from:

```text
cli.run / cli.n7-live
  → tools/n7_live/run_n7_live_oneshot.py
  → run_yaml_live
  → run_operator_oneshot
  → run_live_oneshot_session
  → _run_in_session_mutation_phase
  → N7OneShotHost(...)
  → arm_operator_live()
```

Also removed from dress-rehearsal wrappers.

No dead optional parameters remain on those surfaces.

---

## 5. Safety controls explicitly preserved

1. `--live` required for real venue mutations  
2. Omitting `--live` remains read-only  
3. `TYREX_N7_FORBID_LIVE=1` blocks arming  
4. CI / pytest block arming  
5. Real arm rejects `FakeTransport`  
6. SDK mutation transport starts network-disabled (`arm=None`)  
7. Authenticated read-only preflight remains  
8. Credentials / signer / funder validation remain  
9. User-stream readiness required before arming  
10. Baseline capture + selected-market reconciliation remain  
11. Unresolved-session discovery / recovery remain  
12. Pre-submit freshness / book revalidation remain  
13. Fee-inclusive BUY cap ≤ 5 USDC  
14. Daily notional / loss ≤ 5 USDC  
15. One position / one entry lineage  
16. Same-window re-entry / reversal forbidden  
17. FAK required  
18. Submission obligations / ambiguous handling remain  
19. Settlement / sellability / exit supervision / retries / terminal recon / handoff remain  
20. No config value alone silently enables mutations without `--live`  
21. Internal `MutationArmToken` transport gate remains  

---

## 6. Tests added, removed, and updated

### Added

- `tests/test_admission_simplification.py` — CLI absence of artifact flag; requested vs armed; forbid/CI/pytest; FakeTransport refuse; stream failure; YAML alone insufficient; fee/FAK/lineage; historical extra-field ignore

### Updated / rewritten

- `test_n7_dress_rehearsal.py`
- `test_n7_phase10_readiness.py`
- `test_n7_terminal_safety.py`
- `test_n7_remediation_production_path.py` (section F)
- `test_n7_lifecycle_acceptance.py`

### Removed obsolete behaviors

Tests that required missing artifacts, fingerprint match, single-use consumption, `tiny_live_admitted` defaults/templates, or YAML admission flips as the authorization gate.

---

## 7. Focused test commands and results

```text
.venv\Scripts\python.exe -m pytest ^
  tests/test_admission_simplification.py ^
  tests/test_n7_dress_rehearsal.py ^
  tests/test_n7_phase10_readiness.py ^
  tests/test_n7_terminal_safety.py ^
  tests/test_n7_remediation_production_path.py ^
  tests/test_n7_lifecycle_acceptance.py -q
```

**Result:** `55 passed`

---

## 8. Full-suite command and result

```text
.venv\Scripts\python.exe -m pytest -q
```

**Result:** `1063 passed in 143.55s`

---

## 9. Config-validation result

Canonical sealed materialization:

```text
sealed.live.enabled = False
sealed.live.mutations_enabled = False
tiny_live_admitted absent from sealed.to_dict()
```

Stale argument rejection:

```text
tyrex-pm: error: unrecognized arguments: --admission-artifact x.json
(exit code 2)
```

---

## 10. Confirmation that real venue mutations were zero

- No real `--live` tiny-LIVE experiment was executed.
- Mutation-disabled validation (`--mode live` without `--live`) completed with:

```json
{
  "outcome": "PREFLIGHT_OK_DRY",
  "live_requested": false,
  "operator_live_armed": false,
  "real_venue_mutations": 0
}
```

- Arming unit tests used spies/fakes only.

**Real venue mutations by this task: 0**

---

## 11. Remaining admission/fingerprint references and why

| Location | Justification |
|---|---|
| Historical Phase 2–10 implementation reports under `Docs/implementation/...` | Historical evidence; not current operator instruction |
| `admission_artifact_template.json` / `tiny_live_admission_checklist.md` | Explicitly marked historical / superseded |
| Tests asserting absence of `--admission-artifact` / ignoring legacy sealed extras | Prove removal and persistence compatibility |
| Internal `MutationArmToken(artifact_id=...)` / sealed `.fingerprint()` | Internal automatic mechanisms; not operator ceremony |
| Unused abort enum values `TINY_LIVE_NOT_ADMITTED`, `LIFECYCLE_SAFETY_ABSENT` | Kept for enum stability; no longer emitted by arm path |

Active `src/`, `tools/`, and current operator docs no longer require the ceremony.

---

## 12. Known limitations / red flags

- Historical docs still contain old wording; operators must follow `operator_tiny_live_runbook.md`.
- Unused admission-era abort codes remain in `N7AbortCode` for compatibility.
- Internal sealed fingerprint string values change when sealed payload fields change (expected; automatic only).
- Real tiny-LIVE still requires a healthy operator host, credentials, feeds, and all remaining runtime gates; removing the ceremony does not weaken those gates.

---

## 13. Exact final Git Bash command the owner should run

```bash
python -m tyrex_pm.application.cli run \
  --mode live \
  --strategy config/strategies/z_gap.yaml \
  --risk config/risk/tiny_live_5usd.yaml \
  --execution config/execution/polymarket_live.yaml \
  --runtime config/runtime/live_btc_5m.yaml \
  --run-name tiny_live_validation \
  --live
```

This implementation task did **not** run that command.
