# Operator post-run checklist (Phase 10)

Use after the **one** authorized tiny-live experiment. Not applicable to the readiness-only task (mutations = 0).

## Evidence package (all required)

Checklist helper: `tyrex_pm.runtime.n7_operator_monitor.evidence_checklist_status`.

| # | Artifact | Present? |
|---|---|---|
| 1 | Manifest and sealed configuration fingerprint | ☐ |
| 2 | Complete critical audit stream | ☐ |
| 3 | Candidate and pre-submit revalidation evidence | ☐ |
| 4 | HTTP submission results and user-stream correlations | ☐ |
| 5 | Obligation state history | ☐ |
| 6 | Matched / confirmed / sellable quantity history | ☐ |
| 7 | Lifecycle and supervisor transitions | ☐ |
| 8 | Entry/exit order and trade IDs | ☐ |
| 9 | Final open-order / trade / balance reconciliation | ☐ |
| 10 | Baseline delta | ☐ |
| 11 | Terminal classifier inputs and result | ☐ |
| 12 | Mutation count | ☐ |
| 13 | Operator incident/handoff record if not flat | ☐ / N/A |

## Success criteria (all required for PASS)

- [ ] One permitted entry lineage only
- [ ] Fee-inclusive BUY debit ≤ `$5.00`
- [ ] Every execution applied exactly once
- [ ] No unowned execution attributed to the strategy
- [ ] No owned open order remains
- [ ] Selected-market balance returned to baseline or approved dust
- [ ] Obligations resolved
- [ ] Reconciliation clean
- [ ] Terminal `FLAT_CONFIRMED` or policy-valid `FLAT_WITH_DUST`
- [ ] Reports contain no contradictions
- [ ] Mutations forced off at termination (`mutations_force_off=true`, `mutations_ready=false`)

## Outcome notes

- `NO_FILL_CONFIRMED` is a safe experiment outcome but does **not** validate the full entry→exit path. A separately reviewed repeat requires fresh authorization.
- One successful run does **not** authorize continuous operation or a higher capital limit.
- Phase 10 PASS authorizes **no** automatic second live run.

## If not flat

1. Confirm mutations remain disabled.
2. Preserve handoff report (`operational.operator_handoff`).
3. Use authenticated REST recon only; do not invent inventory.
4. Plan any residual exit with fresh book, sellability, and fee-aware sizing under explicit operator control.
