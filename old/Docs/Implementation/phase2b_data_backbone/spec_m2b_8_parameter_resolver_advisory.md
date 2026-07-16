# M2B.8 — ParameterResolver advisory-only

## 1. Purpose in simple terms

Introduce a pure `ParameterResolver` that computes `EffectiveParams = clamp(static_config ⊕ advisor_overrides, bounds)` and wires it into the monitor in **pass-through mode**. With no advisors registered, static config is returned verbatim. At most **one** advisory table (e.g. DynamicStopPolicy candidate) may compute and log — **never enforce**.

## 2. Boundary

### In scope

- `advisor/resolver.py` — pass-through resolver
- Config bounds schema `{value, min, max}` per parameter
- `advisor_decision` and `advisor_fallback` facts
- Optional one advisory table from notebook calibration
- Clamp/bounds/fallback tests
- Replay-diff still green with resolver active

### Out of scope

- **Enforcement** of advisor overrides (Phase 3.0)
- All advisor modules (only one candidate table)
- `ContinuationRunnerPolicy` (requires notebook 06 positive)
- Changing effective live parameters
- Runner / alpha modules

## 3. Current repo reality

| Path | State |
|------|-------|
| `runtime/config.py` | Static survival/paired_binary params |
| `survival/advisory.py` | Phase 1 mechanical survival — distinct from ParameterResolver |
| M2B.5 | Replay-diff baseline |
| M2B.7 | Label statistics for evidence |

## 4. Files to create

| File | Why |
|------|-----|
| `src/tyrex_pm/advisor/__init__.py` | Advisor package (read-only to live) |
| `src/tyrex_pm/advisor/resolver.py` | Pass-through resolver |
| `src/tyrex_pm/advisor/bounds.py` | Clamp logic |
| `tests/test_parameter_resolver_passthrough.py` | Static verbatim |
| `tests/test_parameter_resolver_bounds.py` | Clamp + fallback |

## 5. Files allowed to modify

| File | Modification |
|------|--------------|
| `strategies/paired_binary/monitor.py` | Call resolver; **ignore** output for decisions in 2B |
| `runtime/config.py` | Bounds schema |
| `reporting/schema_v2.py` | `FACT_TYPE_ADVISOR_DECISION`, `FACT_TYPE_ADVISOR_FALLBACK` |
| `Docs/CONFIG_MODEL.md` | Bounds documentation |

## 6. Forbidden files / modules

```text
risk/engine.py — no bypass
execution/planner.py
survival/enforcement_dispatch.py — no new enforce paths
```

## 7. Interfaces and contracts

```python
class ParameterResolver:
    def resolve(self, ctx: AdvisorContext) -> ResolvedParams:
        """With no advisors: returns static config unchanged.
        Logs advisor_decision only when advisor proposes delta."""

@dataclass(frozen=True)
class ResolvedParams:
    params: Mapping[str, Any]
    source: Literal["static", "advisor", "fallback"]
```

**Hard rule:** `monitor` uses static config for all decisions in M2B.8; resolver output logged only.

## 8. Runtime flags and rollback

```yaml
runtime:
  advisor:
    enabled: false          # DEFAULT
    log_recommendations: true
```

Rollback: `enabled: false` — resolver not invoked.

## 9. Tests required

Pass-through, bounds clamp, missing-feature fallback, fact-diff on/off identical decisions.

## 10. Regression tests required

`test_replay_fact_diff_golden.py` green with resolver enabled in replay only.

Live: fact-diff run resolver on vs off — same intents and risk outcomes.

## 11. Acceptance criteria

- [ ] Zero live behavior change (fact-diff proof)
- [ ] `advisor_decision` emitted only when table proposes non-zero delta
- [ ] `advisor_fallback` on missing/stale features
- [ ] Bounds prevent out-of-range values even if advisor buggy
- [ ] Replay sweep shows advisory ≥ static CVaR on holdout (evidence only — not enforce trigger)

## 12. Divergence risks

| Risk | Control |
|------|---------|
| Monitor reads resolved params | Explicit static-only wiring in 2B |
| Calling this "enforcement" | Phase naming guardrails |
| Multiple advisors | One table max |

## 13. Review checklist

- [ ] Grep monitor for resolved param usage in decisions — must be none
- [ ] Bounds schema in CONFIG_MODEL
- [ ] Replay-diff green

## 14. Done / not done examples

**Done:** `advisor_decision` fact logs proposed stop distance; actual stop unchanged.  
**Not done:** `enforcement_mode: enforce` on advisor output.

## 15. Next milestone dependency

**M2B.9** shadow-live compares advisor recommendations vs outcomes.
