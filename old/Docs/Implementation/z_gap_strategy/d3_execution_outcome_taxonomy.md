# D3 — Fee Curve and Execution Outcome Taxonomy

**Status:** COMPLETED

## D3.a — Fee-curve monotonicity

**Artifact:** `var/reporting/z_gap/d3_fee_curve_validation.json`  
**Script:** `scripts/validate_d3_fee_curve.py`  
**Tests:** `tests/test_fees_phi.py` (grid monotonicity, symmetry, boundaries)

Verified for `fd.r=0.07`, `fd.e=1`, `fd.to=true`:

- φ increases on [0, 0.5]
- φ decreases on [0.5, 1]
- φ(p) = φ(1−p)
- Maximum at p = 0.5
- φ(0) = φ(1) = 0
- Missing `fd` fails closed (unknown model)
- Invalid price outside [0,1] rejected

## D3.b — Entry outcome taxonomy

**Module:** `src/tyrex_pm/strategies/z_gap/execution_outcomes.py`  
**Tests:** `tests/test_z_gap_execution_outcomes.py`

| Category | Fact type | Meaning |
|----------|-----------|---------|
| `blocked` | `z_gap_entry_blocked` | Locally blocked before submission |
| `rejected` | `z_gap_entry_rejected` | Submitted but rejected by planner/risk/OMS/venue |
| `zero_fill` | `z_gap_entry_unfilled` | FAK submitted, zero fill |
| `partial_fill` | `z_gap_entry_partial_fill` | Confirmed partial BUY |
| `full_fill` | `z_gap_entry_fill` | Confirmed full BUY |
| `unresolved` | `z_gap_entry_execution_unresolved` | Ambiguous / pending reconciliation |

Required evidence fields in `to_fact_payload()`: `client_order_id`, `venue_order_id`, `submission_attempted`, `submission_acknowledged`, `reject_stage`, `reject_code`, `requested_quantity`, `confirmed_filled_quantity`, `average_fill_price`, `order_status`, `terminal_interpretation`.

**Runtime wiring:** `src/tyrex_pm/runtime/z_gap_enforce.py` (`_reconcile_entry_pending`) emits `emit_z_gap_entry_outcome` and sets `lifecycle.entry_outcome_category`.

**Terminal summary:** `entry_outcome_category` field in `z_gap_terminal_summary`.
