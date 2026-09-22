# Implementation evidence

Chronological reports, checklists, and run artifacts from earlier phases.

**Not current API.** Operator and developer documentation lives in [`../latest/`](../latest/README.md).

**Next build sequence:** [first_release/](first_release/README.md) — R1–R5 then R7 for a bounded first release (one-shot z_gap, $5 cap). R6 `--continue` and F-L2 (L2/`feature_probe` freshness) are deferred. Does not add strategies, connectors, families, or a backtester.

Prior seam work: [grounding_framework/grounding_plan.md](grounding_framework/grounding_plan.md) — F1–F5 landed. Venue grounding G1–G8: [grounding_fixes/](grounding_fixes/README.md). Operator-saved 2026-09-07/08 runs include SL proof on a prior revision; first-release live proof is still sha-specific (see first_release R7). Plan vs offline vs live acceptance are separate gates.

Some older R-series acceptance reports were removed from this tree. Remaining folders (event-driven lifecycle, reporting, book state, runtime enhancement) are evidence of how the unified runtime was built; they may name hosts, YAML paths, or modules that no longer exist.
