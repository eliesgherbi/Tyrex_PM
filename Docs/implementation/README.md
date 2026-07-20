# Implementation evidence

Chronological phase reports, incidents, live-validation evidence, and acceptance notes.

**Current accepted checkpoint:** R8 — see [r8_framework_acceptance.md](r8_framework_acceptance.md).  
**Current how-to docs:** [`../latest/`](../latest/README.md).  
**Z-Gap decision path:** F1–F5 accepted (fixture OBSERVE/SHADOW + resolution shadow).  
**Next major initiative:** [Z-Gap production readiness (N1–N7)](z_gap_production_readiness/README.md).

Key anchors:

| Document | Role |
|----------|------|
| [r8_framework_acceptance.md](r8_framework_acceptance.md) | Framework acceptance matrix |
| [r7_successful_live_acceptance.md](r7_successful_live_acceptance.md) | Third live success evidence |
| [r7f_operator_runbook.md](r7f_operator_runbook.md) | Closed R7 runbook |
| [r7_exit_floor_policy.md](r7_exit_floor_policy.md) | Exit-floor formulas |
| [r7b_first_live_incident.md](r7b_first_live_incident.md) | Live 1 — settlement race |
| [r7d2_second_live_incident.md](r7d2_second_live_incident.md) | Live 2 — wrong SELL price |

### Z-Gap P0 design baseline (pre-implementation)

| Document | Role |
|----------|------|
| [z0_z_gap_design_audit.md](z0_z_gap_design_audit.md) | Legacy Phase A evidence audit (what was implemented) |
| [z0_z_gap_full_strategy_spectrum.md](z0_z_gap_full_strategy_spectrum.md) | Accepted full economic strategy (corrections A–F) |
| [z_gap_full_strategy_implementation_plan.md](z_gap_full_strategy_implementation_plan.md) | Architecture + F1–F5 implementation preparation |

### Z-Gap implementation milestones (accepted)

| Document | Role |
|----------|------|
| [f1_generic_strategy_contracts.md](f1_generic_strategy_contracts.md) | F1 — neutral `StrategyDecision` / `IntentLike` / protocol |
| [f2_z_gap_model_and_policies.md](f2_z_gap_model_and_policies.md) | F2 — PTB/time/indicators/valuations/policies (pure) |
| [f3_z_gap_observe.md](f3_z_gap_observe.md) | F3 — fixture OBSERVE wiring (no OMS) |
| [f4_z_gap_shadow_lifecycle.md](f4_z_gap_shadow_lifecycle.md) | F4 — fixture SHADOW entry/exit lifecycle |
| [f5_z_gap_resolution_shadow.md](f5_z_gap_resolution_shadow.md) | F5 — resolution-aware fixture SHADOW |

P0–F5 documents remain in place; they are not part of the N1–N7 folder reorganization.

---

## Z-Gap production readiness (one initiative)

N1–N7 are milestones of a **single** production-readiness initiative.

**Master index:** [z_gap_production_readiness/README.md](z_gap_production_readiness/README.md)

| Milestone | Document |
|-----------|----------|
| N1 | [n1_source_and_legacy_audit.md](z_gap_production_readiness/n1_source_and_legacy_audit.md) |
| N2 | [n2_real_data_adapters.md](z_gap_production_readiness/n2_real_data_adapters.md) |
| N3 | [n3_ptb_and_reference_alignment.md](z_gap_production_readiness/n3_ptb_and_reference_alignment.md) |
| N4 | [n4_real_observe.md](z_gap_production_readiness/n4_real_observe.md) |
| N5 | [n5_real_data_shadow.md](z_gap_production_readiness/n5_real_data_shadow.md) |
| N6 | [n6_live_execution_and_reconciliation.md](z_gap_production_readiness/n6_live_execution_and_reconciliation.md) |
| N7 | [n7_tiny_operator_live.md](z_gap_production_readiness/n7_tiny_operator_live.md) |

Roadmap, critical path, mode progression, decision gates, readiness vs calibration,
and open decisions live in the initiative README — not as separate top-level
initiatives under `Docs/implementation/`.

---

All files in this directory are preserved as historical evidence (plus the P0
design baseline, F1–F5 milestone reports, and the
[z_gap_production_readiness](z_gap_production_readiness/README.md) initiative).
