# M2B.9 — Shadow-live validation

## 1. Purpose in simple terms

Run advisors in **advisory-only** mode on live scenarios for at least one week. Nightly job diffs advisor recommendations against static-outcome baselines using the day's recordings. Confirms shadow behavior matches replay expectations before any Phase 3 enforce promotion.

## 2. Boundary

### In scope

- Live scenarios with `advisor.enabled: true`, `log_recommendations: true`
- Nightly shadow diff script in `scripts/`
- Weekly shadow report artifact
- Tolerance checks vs replay predictions
- Fallback storm detection

### Out of scope

- Enforcement
- Parameter changes to production scenarios from shadow results
- Phase 3.0 work

## 3. Current repo reality

| Path | State |
|------|-------|
| M2B.8 | ParameterResolver advisory wiring |
| M2B.1-B | Daily recordings for diff input |
| M2B.5 | Replay baseline for same days |

## 4. Files to create

| File | Why |
|------|-----|
| `scripts/shadow_advisor_diff.py` | Nightly comparison |
| `config/scenarios/shadow_paired_binary_advisory.yaml` | Shadow scenario |
| `tests/test_shadow_advisor_diff.py` | Unit test on fixture day |

## 5. Files allowed to modify

`Docs/OPERATIONS.md` — shadow run protocol.

## 6. Forbidden files / modules

```text
survival/enforcement.py enforce paths
advisor/resolver.py — no enforce switch
```

## 7. Interfaces and contracts

### Shadow report

```text
date, advisor_name, recommendation_count, fallback_count,
mean_delta_vs_static, replay_tolerance_pass, notes
```

Tolerance: recommendations within X% of replay-predicted distribution.

## 8. Runtime flags and rollback

Shadow scenario only — not default production. Kill: stop shadow scenario.

## 9. Tests required

Fixture day diff; fallback storm threshold alert.

## 10. Regression tests required

Production scenarios unchanged; shadow is opt-in.

## 11. Acceptance criteria

- [ ] ≥1 week shadow runs completed
- [ ] Nightly diff job green within tolerance
- [ ] No fallback storms (>N per hour configurable)
- [ ] Latency budget held (advisor compute < budget)
- [ ] Written sign-off memo for Phase 3 readiness (not auto-promote)

## 12. Divergence risks

| Risk | Control |
|------|---------|
| Accidental enforce | Scenario review |
| Shadow becomes default live | Separate scenario file naming |

## 13. Review checklist

- [ ] Shadow scenario clearly named `shadow_*`
- [ ] Report artifacts archived
- [ ] No enforce facts in shadow runs

## 14. Done / not done examples

**Done:** 7 daily shadow reports within replay tolerance.  
**Not done:** Flip `DynamicStopPolicy` to enforce.

## 15. Next milestone dependency

**Phase 3.0** may begin only after explicit approval + this milestone sign-off.
