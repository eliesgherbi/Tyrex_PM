# Phase 3.0 — Controlled adaptive enforcement (future only)

## 1. Purpose in simple terms

**Future phase — not part of Phase 2B implementation.** Document the boundary for when adaptive advisors transition from advisory logging to **live enforcement**, one module at a time, using the Phase 1 advisory→enforce promotion discipline.

## 2. Boundary

### In scope (documentation only in this package)

- Promotion ladder definition
- Per-module experiment scenario pattern
- Kill-switch revert to static config
- Prerequisites checklist from M2B.9

### Out of scope

- Any implementation in Phase 2B program
- Any enforcement code without separate Phase 3 approval
- Batch enforcement of multiple advisors

## 3. Current repo reality

| Path | State |
|------|-------|
| Phase 1 pattern | `enforcement_mode: advisory` → `enforce` in scenarios (trailing enforce exists) |
| M2B.8 | ParameterResolver pass-through only |
| M2B.9 | Shadow validation — prerequisite |

## 4. Files to create

None in Phase 2B. Future: `Docs/Implementation/phase3_adaptive_enforcement/` when approved.

## 5. Files allowed to modify

None during Phase 2B.

## 6. Forbidden files / modules

```text
All Phase 2B milestones — no Phase 3 enforce wiring
```

## 7. Interfaces and contracts

### Promotion ladder (future)

```text
1. Replay-validated on holdout (M2B.5 evidence)
2. Shadow-advisory live ≥1 week (M2B.9)
3. Controlled enforce in experiment scenario with kill switch
4. Validator + replay-diff green on promotion run itself
5. One module at a time — start with DynamicStopPolicy (damage control)
```

### EffectiveParams enforce (future)

```python
# ONLY in Phase 3.0 with explicit scenario flag:
monitor uses resolver.resolve().params for decisions  # NOT in 2B
```

## 8. Runtime flags and rollback

Future: `advisor.<module>.enforcement_mode: enforce` in dedicated experiment scenarios only.

Rollback: scenario kill switch → `static` instantly.

## 9. Tests required (future)

Per-module enforce validation; replay-diff on promotion run; live fact-diff vs shadow.

## 10. Regression tests required (future)

Phase 2B golden replay-diff must remain green when enforce scenarios are **not** active.

## 11. Acceptance criteria (future)

- [ ] Separate Phase 3 program approved by human
- [ ] One module promoted with full ladder
- [ ] Kill switch tested in live shadow box

## 12. Divergence risks

| Risk | Control |
|------|---------|
| Implementing enforce in M2B.8 | Spec forbids |
| Skipping shadow week | M2B.9 prerequisite |
| ContinuationRunner without notebook 06 go | Plan §15 last module |

## 13. Review checklist

- [ ] Confirm milestone ID is Phase 3.0 not 2B
- [ ] No enforce code in 2B PRs

## 14. Done / not done examples

**Not done in Phase 2B:** Any `enforcement_mode: enforce` on advisor output.  
**Future done:** DynamicStopPolicy enforce scenario validated over 3 live runs.

## 15. Next milestone dependency

Requires **M2B.9** sign-off + **explicit human approval** to open Phase 3 program.

**Phase 2B ends at M2B.9.** Phase 3.0 is a new program.
