# Position Reconciliation Validation Runbook

Two-phase validation: **shadow mode** (observe-only) then **live mode** (engine mutation).

---

## Phase 1 — Shadow Mode

### Run

```bash
python scripts/run_guru.py \
  --strategy-conf config/scenarios/position_reconciliation_validation/guru_follow.yaml \
  --risk-conf config/scenarios/position_reconciliation_validation/guru_follow_risk.yaml \
  --live-conf config/scenarios/position_reconciliation_validation/live_polymarket_shadow.yaml
```

### What to expect

The reconciliation pass runs on every wallet sync cycle (every 15 s) but does **not** send `PositionStatusReport` to the engine. It emits `position_reconciliation` facts with `reconciliation_sent: false`.

### Validation steps

1. **Confirm reconciliation is running.** Grep the Tyrex log for:

   ```
   grep "event=position_reconciliation" <tyrex_log>
   ```

   You should see one log line per instrument per cycle where a diff was detected. If no positions are open or all match, no log lines appear (expected).

2. **Confirm facts are emitted.** Inspect the facts JSONL:

   ```
   grep "position_reconciliation" var/reporting/runs/<run_id>/facts.jsonl
   ```

   Each fact should contain: `cycle`, `instrument_id`, `venue_qty`, `cache_qty`, `diff_direction`, `deferred`, `defer_count`, `reconciliation_sent`.

3. **Confirm shadow mode is active.** Every `position_reconciliation` fact must have:

   ```
   "reconciliation_sent": false
   ```

   If any fact has `reconciliation_sent: true`, shadow mode is broken — stop and investigate.

4. **Validate diff accuracy.** For each `position_reconciliation` fact:
   - `venue_qty` should match what the Data API reports for that token.
   - `cache_qty` should match the position visible in Nautilus cache.
   - `diff_direction` should be `"stale_close"` (venue=0, cache>0), `"stale_partial"` (0 < venue < cache), or `"venue_has_more"` (venue > cache).
   - Verify at least one `stale_close` or `stale_partial` fact by manually selling a position via the Polymarket UI during the run, then waiting 2-3 cycles.

5. **Verify deferral behavior.** If a real fill just happened (within `data_api_lag_tolerance_seconds = 60s`), the reconciliation should defer:

   ```
   grep "event=position_reconciliation_deferred" <tyrex_log>
   ```

   The `defer_count` should increment each cycle. After `position_reconciliation_deferral_max` (5) consecutive deferrals, it proceeds:

   ```
   grep "event=position_reconciliation_stuck" <tyrex_log>
   ```

6. **Verify no engine mutation.** Confirm that `Cache.positions_open()` is unchanged by the reconciliation pass — positions that were "stale" in cache should remain stale (shadow mode skips `msgbus.send`). Check the deployment budget facts: the portfolio deployment should stay at the pre-reconciliation level.

7. **Verify health source.** If a deferral gets stuck (defer_count >= deferral_max), the health source should report:

   ```
   grep "position_reconciliation_stuck" <tyrex_log>
   ```

   This surfaces as `DEGRADED_OMS` in `tradable_state_health` facts.

### Pass criteria (shadow mode)

- [ ] `position_reconciliation` facts emitted with correct `venue_qty` / `cache_qty` / `diff_direction`.
- [ ] All facts have `reconciliation_sent: false`.
- [ ] At least one external close or partial was correctly detected.
- [ ] Deferral logic works (defers when position was recently modified).
- [ ] Cache positions are **not** mutated (deployment budget unchanged).
- [ ] No errors or tracebacks related to reconciliation in the log.

### Fail criteria

- Any `position_reconciliation` fact has incorrect `venue_qty` or `cache_qty`.
- Facts show `reconciliation_sent: true` (shadow mode broken).
- Errors or tracebacks in reconciliation code path.
- Health source incorrectly reports stuck deferrals when none exist, or misses real stuck deferrals.

---

## Phase 2 — Live Mode

**Prerequisite:** Phase 1 passed. Shadow-mode facts validated for at least one session with external closes.

### Run

```bash
python scripts/run_guru.py \
  --strategy-conf config/scenarios/position_reconciliation_validation/guru_follow.yaml \
  --risk-conf config/scenarios/position_reconciliation_validation/guru_follow_risk.yaml \
  --live-conf config/scenarios/position_reconciliation_validation/live_polymarket_live.yaml
```

### Test scenario: Portfolio cap unblock via reconciliation

This is the primary scenario the feature was built for.

1. **Fill to cap.** Let the bot open positions until `max_portfolio_notional_usd_open` (25 USD) is reached. Verify risk denials appear:

   ```
   grep "portfolio_cap" <tyrex_log>
   ```

2. **External close.** Sell one or more positions via the Polymarket UI (not Tyrex). Wait 2-3 wallet sync cycles (~30-45 s).

3. **Observe reconciliation.** Grep for:

   ```
   grep "event=position_reconciliation_sent" <tyrex_log>
   ```

   You should see log lines for each externally-closed position with `direction=stale_close`.

4. **Verify cache mutation.** After the reconciliation fact with `reconciliation_sent: true`:
   - The position should no longer appear in `Cache.positions_open()`.
   - The `capital_snapshot` fact should show reduced `portfolio_deploy_usd`.

5. **Verify cap unblock.** The next guru signal should pass risk (portfolio deployment now below cap). Look for:

   ```
   grep "strategy_decision" <tyrex_log> | grep "action=submit"
   ```

   If no guru signal arrives during the test window, verify that the deployment budget fact shows the freed capacity.

6. **Verify facts.** `position_reconciliation` facts should now have `reconciliation_sent: true` for actions that were applied.

7. **Verify idempotence.** On the cycle after reconciliation, the same instrument should not trigger another action (it's in `_recently_reconciled` for `recently_reconciled_ttl_seconds`). Grep for:

   ```
   grep "event=position_reconciliation_skipped_ttl" <tyrex_log>
   ```

### Pass criteria (live mode)

- [ ] Externally-closed positions are detected and reconciled (facts with `reconciliation_sent: true`).
- [ ] Cache position is removed after reconciliation — `portfolio_deploy_usd` decreases.
- [ ] Portfolio cap unblocks — new trades are accepted after reconciliation frees capacity.
- [ ] Recently-reconciled TTL prevents duplicate reconciliation on subsequent cycles.
- [ ] No errors or tracebacks in the reconciliation code path.
- [ ] PnL on reconciled positions is approximate (expected — see `00_overview.md` Known accuracy trade-offs).

### Fail criteria

- Reconciled position still appears in cache after `msgbus.send`.
- Portfolio deployment doesn't decrease after reconciliation.
- Duplicate reconciliation actions on consecutive cycles for the same instrument.
- Engine crash or unhandled exception from synthetic `PositionStatusReport`.
- Health source reports false positives or misses genuine stuck deferrals.

---

## Log lines reference

| Event | Meaning |
|---|---|
| `event=position_reconciliation_sent` | Live mode: report sent to engine |
| `event=position_reconciliation_deferred` | Deferred due to Race B (ts_last) or Race C (in-flight orders) |
| `event=position_reconciliation_stuck` | Deferral limit reached, proceeding anyway |
| `event=position_reconciliation_skipped_ttl` | Recently reconciled, skipped (Race E) |
| `event=position_reconciliation_no_account` | Account not in cache, skipped action |

## Fact types reference

| Fact type | Key fields |
|---|---|
| `position_reconciliation` | `cycle`, `instrument_id`, `venue_qty`, `cache_qty`, `diff_direction`, `deferred`, `defer_count`, `reconciliation_sent` |
| `tradable_state_health` | `level`, `reason_code` (look for `position_reconciliation_stuck`) |
| `capital_snapshot` | `portfolio_deploy_usd` (verify decrease after reconciliation) |
