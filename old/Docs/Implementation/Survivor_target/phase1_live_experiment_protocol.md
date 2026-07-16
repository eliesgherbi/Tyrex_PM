# Phase 1 live experiment protocol

Goal: measure whether survival reduces damage when the strategy is wrong.

**PnL:** Use Polymarket UI manual PnL as temporary source of truth when `pnl_status: tentative`. Bot PnL is for structure only until fill reconciliation is final.

## Run profiles

| Profile | Scenario | Purpose |
|---------|----------|---------|
| A. Advisory baseline | `live_paired_binary_phase1_advisory` | Facts only, baseline damage |
| B. Target-only | `live_paired_binary_phase1_target_only` | Dynamic target downgrade alone |
| C. Trailing enforce | `live_paired_binary_phase1_trailing_enforce` | Protect favorable survivor moves |
| D. Stall enforce | `live_paired_binary_phase1_stall_enforce` | Downgrade (default) or exit on stall |

Run **one enforce module at a time**. Never enable all enforce modes.

## Preflight (required)

```bash
python scripts/preflight_phase1_live_scenario.py \
  --scenario live_paired_binary_phase1_advisory \
  --event-url "https://polymarket.com/event/btc-updown-5m-<START_TS>"
```

## Per-run collection

- PM UI manual PnL (authoritative for performance)
- Bot `pnl_status`, `pnl_blocker_for_enforcement`
- `final_state`, loser leg, loser exit price (UI)
- `target_mode`, full_recovery vs selected target
- Survivor reached TP? trailing armed/triggered? stall detected?
- `survival_enforce_exit_submitted`? exit reason
- Time in survivor phase
- Validator classification

## Success metrics (damage control)

- Smaller loss when survivor stalls
- Earlier exit after favorable-then-reverse moves (trailing enforce)
- Fewer runtime/pre-close forced exits vs wrong-way hold
- Lower average loss on wrong runs; fewer large negative outliers
- Clear exit reason in facts

## Validator classifications

- `PHASE1_ADVISORY_PASS` / `PHASE1_TARGET_POLICY_PASS`
- `PHASE1_TRAILING_ENFORCE_PASS` / `PHASE1_STALL_ENFORCE_PASS`
- `PHASE1_ENFORCE_SAFETY_FAIL` — duplicate enforce submits, etc.

Lifecycle pass is **not** blocked by tentative PnL.

## Post-run

```bash
python scripts/validate_paired_binary_phase2_live_run.py var/reporting/runs/<run_name>
python scripts/replay_survival_advisory.py var/reporting/runs/<run_name>
```

Optional manual PnL check:

```bash
python scripts/reconcile_run_cashflows.py --facts var/reporting/runs/<run>/facts.jsonl --manual-fill ...
```
