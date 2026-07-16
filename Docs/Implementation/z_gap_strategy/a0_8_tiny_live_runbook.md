# A0.8 Z-Gap Tiny Live Enforce Runbook

**Phase:** A (operational validation only)  
**Cap:** `$5` fixed USD, one position per window, FAK entry/exit  
**No automatic restart recovery** — manual reconciliation required after crash or FAILED state.

---

## 0. Single-command orchestrator (preferred)

The operator **does not supply K manually**. One process discovers the next window, warms feeds, supervises the Chainlink sidecar, collects interactive approvals, waits for the boundary, captures and locks K, then runs the window.

### Operator command (after commissioning)

```bash
python scripts/go_z_gap_tiny_live.py \
  --next-window \
  --run-name "z_gap_first_tiny_live" \
  --execute
```

Read-only / CI validation (never approves, never trades):

```bash
python scripts/go_z_gap_tiny_live.py \
  --next-window \
  --run-name "z_gap_commissioning_check" \
  --non-interactive
```

Observe-only commissioning (no orders):

```bash
python scripts/go_z_gap_tiny_live.py \
  --next-window \
  --run-name "z_gap_commissioning_observe" \
  --observe-only
```

### Timeline

| Time | Action |
|------|--------|
| **T−90s** | Next BTC 5m window selected (`target_prestart_seconds: 90`); feeds + sidecar start |
| **T−60s** | Metadata, clock, Binance, fee descriptor, books warming; operator approvals |
| **T−20s** | Hard minimum prestart confirmed (`hard_min_prestart_seconds: 20`) |
| **T=0** | K captured from live RTDS + sidecar log; D2 precedence; K locked |
| **T+** | Entry evaluation (at most one $5 FAK if `--execute` and all gates pass) |
| **terminal** | Position closed or no-trade; process stops (no next window) |

### Session statuses

| Status | Meaning |
|--------|---------|
| `READY_TO_WAIT_FOR_BOUNDARY` | Static pre-boundary gates passed; PTB = `WAITING_FOR_BOUNDARY` (not a failure) |
| `PTB_LOCKED_READY_TO_EVALUATE` | Boundary K locked; entry evaluation enabled |
| `NO_TRADE` | Boundary PTB gate failed — no order |
| `NOT_READY` | Static gate failure before boundary |

### Interactive approvals (not auto-generated)

1. **Calibration** (version-scoped, reusable until expiry): type exactly `ACCEPT TINY OPERATIONAL TEST`
2. **Window risk** (per window): type exactly `APPROVE ONE $5 TRADE FOR <MARKET_ID>`

Skipped windows emit `z_gap_window_skipped_insufficient_prestart`.

### PTB attestation policy

No automated independent Polymarket boundary-K API exists. Enforce uses:

- **Exact-window:** live RTDS + sidecar log agreement and lock (≤0.5 bps)
- **Plus:** valid `ptb_commissioning_certificate.json` (`z_gap_ptb_commissioning_v1`: ≥3 recent windows, all ≤0.5 bps, ≤24h fresh, matching PTB config hash)

Boundary attestation is written automatically to `var/reporting/z_gap/ptb_attestation.json` — not required before the window starts.

### Scheduler settings (`live_z_gap_tiny.yaml`)

```yaml
live_validation:
  target_prestart_seconds: 90
  hard_min_prestart_seconds: 20
```

Windows with lead time below target are skipped; never join late.

---

## 1. Legacy manual preflight (superseded by §0 for tiny live)

1. **No non-terminal persisted lifecycle** — delete or archive `var/reporting/z_gap/lifecycle_state.json` if phase is not `DONE`/`FAILED`/`IDLE`, or reconcile venue manually first.
2. **Fresh PTB attestation** (live recording, not golden-day only):

```bash
python scripts/preflight_ptb_attestation.py --output var/reporting/z_gap/ptb_attestation.json
```

3. **TimeAuthority / clock sanity:**

```bash
python scripts/preflight_clock_sanity.py --output var/reporting/z_gap/clock_sanity.json
```

4. **Binance connectivity:**

```bash
python scripts/preflight_binance_connectivity.py --output var/reporting/z_gap/binance_connectivity.json
```

5. **Fee curve spike** artifact at `var/reporting/z_gap/fee_curve_spike.json` (from prior Phase A work).
6. **Calibration-lite operator review** (not a statistical pass):

```json
// var/reporting/z_gap/calibration_lite_review.json
{
  "reviewed": true,
  "reviewed_by": "operator",
  "reviewed_at": "<ISO8601>",
  "status": "accepted_for_tiny_live",
  "operator_signoff": true,
  "notes": "Operational validation only; no statistical edge claim."
}
```

7. **Operator enforce approval** (operator-authored only — never auto-generated):

```json
// var/reporting/z_gap/operator_enforce_approval.json
{
  "approved": true,
  "market_id": "btc_5m_<YYYYMMDD_HHMM>",
  "maximum_usd": "5",
  "one_trade_only": true,
  "approval_ts": <unix>,
  "expiration_ts": <unix>,
  "manual_kill": false
}
```

8. Edit `config/scenarios/live_z_gap_tiny.yaml` with real `market_id`, `condition_id`, token IDs, and `event_start_ts` / `event_end_ts` for the target window.

---

## 2. Preflight (mandatory before live)

```bash
python scripts/preflight_z_gap_live_scenario.py \
  --scenario config/scenarios/live_z_gap_tiny.yaml \
  --artifacts-dir var/reporting/z_gap
```

Expect `PREFLIGHT OK`. Any `ERROR:` line blocks enforce startup.

The script prints a per-gate summary (`fee_curve`, `binance_connectivity`, `time_authority`, `fresh_ptb_attestation`, `calibration_review`, `operator_approval`) before errors. Dry-run against the template scenario without operator artifacts should show `PREFLIGHT BLOCKED` with operator approval and fresh PTB as the remaining blockers after technical gates pass.

Optional JSON report:

```bash
python scripts/preflight_z_gap_live_scenario.py \
  --scenario config/scenarios/live_z_gap_tiny.yaml \
  --json-out var/reporting/z_gap/preflight_report.json
```

---

## 3. Shadow E2E dry-run (mandatory before first live authorization)

Validates the full enforce lifecycle with `ScenarioOMS` (no real orders). Closure pass also runs cumulative regression and venue-lag scenarios with `sell_requires_venue_position: true`:

```bash
python -m pytest tests/test_z_gap_closure_final.py -q
python scripts/run_z_gap_shadow_e2e.py --out-dir var/reporting/runs/z_gap_a0_8_shadow_e2e
```

Confirm `operational_pass: true`, `terminal_state: DONE`, `allocation_zero: true` in printed terminal summary. Facts: `var/reporting/runs/z_gap_a0_8_shadow_e2e/facts.jsonl`.

---

## 4. Live tiny enforce run (operator-authorized only)

**Do not run until preflight OK, shadow E2E OK, and explicit operator authorization.**

```bash
python -m tyrex_pm.runtime.app run \
  --strategy config/strategies/z_gap.yaml \
  --scenario config/scenarios/live_z_gap_tiny.yaml \
  --run-name z_gap_tiny_enforce_<YYYYMMDD_HHMM>
```

Requires `pip install tyrex-pm[live]` and Polymarket credentials in `.env`.

Run artifacts: `var/reporting/runs/z_gap_tiny_enforce_<YYYYMMDD_HHMM>/facts.jsonl`.

---

## 5. Manual kill and intervention

### 5.1 Manual kill before entry

Set in operator approval artifact:

```json
"manual_kill": true
```

Or remove/rename `operator_enforce_approval.json` and restart — preflight blocks OMS entry.

### 5.2 Emergency exit while ACTIVE

1. Set `manual_kill: true` in `operator_enforce_approval.json` **or** stop the process (`Ctrl+C`).
2. Inspect venue position and allocation:

```bash
python scripts/reconcile_run_cashflows.py --run-dir var/reporting/runs/<run_name>
```

3. If position remains, use Polymarket UI or a one-shot reduce-only exit via an approved emergency scenario (paired-binary patterns in `config/scenarios/live_validation_urgent_exit.yaml` are **not** Z-Gap — prefer manual venue flatten for Phase A).

### 5.3 Exit retries exhausted (`FAILED`, `manual_intervention_required`)

Terminal summary retains `remaining_quantity` and `active_quantity`. **Do not restart** normal Z-Gap enforce mode.

1. Read `z_gap_terminal_summary` and `z_gap_reconciliation_*` facts in the run directory.
2. Reconcile venue wallet vs allocation ledger manually.
3. Flatten residual on venue; archive `lifecycle_state.json` only after venue qty = 0.

### 5.4 Venue/wallet mismatch

When `z_gap_reconciliation_failed` or `reconciliation_status: unresolved`:

- Trust order: confirmed fills → allocation → venue snapshot.
- Do not submit additional SELLs above `min(allocation, lifecycle active qty)`.
- Wait `reconciliation.venue_sync_grace_ms` (default 3000 ms) before failing closed.

### 5.5 Process crash with non-terminal persisted state

1. **Do not** restart `live_z_gap_tiny` until manual review.
2. Check `var/reporting/z_gap/lifecycle_state.json` phase.
3. Inspect venue + allocation; flatten manually if needed.
4. Clear or update lifecycle JSON to terminal state only after confirmation.

---

## 6. Post-run verification

```bash
python scripts/analyze_z_gap_observe_runs.py --run-dir var/reporting/runs/<run_name>
```

Required terminal fields: `operational_pass`, `allocation_zero`, `remaining_quantity`, `reconciliation_status`, `manual_intervention_required`, `terminal_state`.

---

## 7. Explicit prohibitions (Phase A)

- Operator window approval and calibration acknowledgment require exact interactive phrases (never silent).
- No `max_usd` above `5`.
- No `run_continue` / multi-window chaining after execution begins.
- No restart recovery after `FAILED` or crash with open position.
- `--non-interactive` must never approve or execute.
