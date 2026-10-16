# settlement_value — operator runbook for the real runs

Date: 11 October 2026. None of the steps below has been run. All automated tests use fakes and recorded data; nothing here was exercised against live providers. Run the steps in order. Each step lists what it validates and what to check before moving on.

Plan: [research_to_live_plan.md](research_to_live_plan.md). Data status: [s0_data_interpretation.md](s0_data_interpretation.md). Candidate settings: [settlement_value_candidate_ranges.md](settlement_value_candidate_ranges.md).

## 1. Longer recording (more windows for S2)

```bash
run_data_collection_live --profile btc-5m --validate-config
run_data_collection_live --profile btc-5m --root var/warehouse_btc5m_s2 --duration 24h
tyrex-data status --root var/warehouse_btc5m_s2 --collection <collection-id>
tyrex-data verify --root var/warehouse_btc5m_s2 --collection <collection-id>
```

**Check:** a clean exit (0), `integrity_ok`, and the number of windows. Then add the collection to `strategy_lab.batch.all_collections()`, rerun `s2_experiments split` (the previous holdout stays a holdout), and rebuild the cache:

```bash
python -m tyrex_pm.research.strategy_lab.s2_experiments split
python -m tyrex_pm.research.strategy_lab.s2_experiments vol --workers 6
python -m tyrex_pm.research.strategy_lab.s2_experiments grid --workers 6
```

Each worker holds about 2.5 GB. Use 4–6 workers on a 32 GB machine. A cold cache build is I/O-bound, and fewer workers are faster.

## 2. Signal-only observation (S4) — no orders

```bash
tyrex-pm observe --config config/observe/settlement_value_observe.yaml --validate-config
tyrex-pm observe --config config/observe/settlement_value_observe.yaml --connect-public-feeds
```

Run it **at the same time as** a collector session (step 1). That way the live RTDS reference (`observation_manifest.json` → `reference_series`) and the PolyBolt TWAP are recorded over the same windows; this is the open S0 item 3.

**Check in `var/observation/<run>_<UTC>/`:**

- **`observation_summary.json`:**
  - `rollovers` should equal `max_windows`;
  - `records_dropped` should be 0;
  - `evaluate_ms_p99` is the local decision latency.
- **`decisions.jsonl`:**
  - the share of `HOST_SKIPPED` rows and their reasons (seal, basis, books);
  - `k_quality`;
  - `twap_reason` (proxy-model readiness);
  - sigma values against the offline range.
- **`recommendations.jsonl`:** frequency, side and price. Compare each with the recorded outcome offline; these are hypothetical, not fills.
- **Offline comparison:** load `decisions.jsonl` with pandas (same schema `settlement_decision_record_v1`) and compare it against a replay of the same windows from the collector session.

## 3. Supervised small-money trial (S6) — real orders

**Preconditions:**

- S0 semantics established for the contract (rule, K, ties);
- observation reviewed;
- a candidate configuration chosen from the ranges note;
- `settlement.redemption_wallet` set in `config/runs/settlement_value_trial.yaml`;
- `TYREX_POLYGON_RPC_URL` set to a real Polygon endpoint;
- account credentials as for any live run.

```bash
tyrex-pm run --config config/runs/settlement_value_trial.yaml --validate-config
tyrex-pm run --config config/runs/settlement_value_trial.yaml --check-account
tyrex-pm settlement --config config/runs/settlement_value_trial.yaml status
tyrex-pm run --config config/runs/settlement_value_trial.yaml --live
```

One process makes at most one entry episode. **Check in `var/runs/settlement_value_trial_<UTC>/` (`run_summary.json` and the run evidence):**

- **`SETTLEMENT_EPISODE_RESERVED`:** the budget figures, and the fee schedule read from Gamma.
- **`INTENT_BLOCKED` / `SETTLEMENT_ENTRY_BLOCKED`:** each reason (`FEE_SCHEDULE_*`, `STRATEGY_FEE_CURVE_MISMATCH`, `TRIAL_LOSS_BUDGET`, …).
- **`SETTLEMENT_ENTRY_OUTCOME`:** `FILL_CONFIRMED`, `DISPATCH_NO_FILL` or `DISPATCH_UNKNOWN`.
- **`HOLD_TO_RESOLUTION`:**
  - with `committed: true`;
  - or a `detail` explaining why not, in which case the pre-commit fallback flattens the position.
- **`SHUTDOWN_HOLD_PRESERVED`:** the holding was not sold.

## 4. Resolution and operator redemption

After the market resolves (in recorded data typically about 1 minute after close, plus finality):

```bash
tyrex-pm settlement --config config/runs/settlement_value_trial.yaml reconcile --connect-chain
tyrex-pm settlement --config config/runs/settlement_value_trial.yaml worklist
```

- **Zero payout:** the episode completes as a loss.
- **Positive payout:** redeem the listed position in the Polymarket UI, then:

```bash
tyrex-pm settlement --config config/runs/settlement_value_trial.yaml confirm-redemption \
    --episode settlement_value_trial:<window_slug> --tx-hash 0x<hash> --connect-chain
tyrex-pm settlement --config config/runs/settlement_value_trial.yaml status
```

A redemption is confirmed only when all of these hold:

- a finalized, successful receipt;
- a Conditional Tokens `PayoutRedemption` log for `redemption_wallet`;
- this condition and this outcome's index set;
- a credited amount equal to the receivable.

A rejection lists the reasons, and the receivable stays a receivable.

The next trial run starts only after startup reconciliation stops reporting `AWAITING_RESOLUTION` / `COMMITTED_HOLD_AWAITING_RESOLUTION` for the previous holding.

## 5. Recovery

- **A crash between reservation and a known dispatch outcome** (`DISPATCH_UNKNOWN`, or `RESERVED` with no outcome): new entries are blocked (`SETTLEMENT_EPISODE_NEEDS_RECONCILIATION`). Inspect the venue order and wallet and the run evidence first. Then either let the normal run's startup reconciliation resolve the session, or record the episode as `mark-manual` with the evidence. A manual episode keeps blocking entries and counts its full worst case against the trial budget.
- **Never delete** `var/runtime_state/settlement/*.jsonl` or the execution journal to clear a blocker.

## 6. Continuous paper soak (retry + hold) — no real orders

Added 16 October 2026. Exercises the continuous runner with fake money: live public feeds, the production runtime, and the simulated account/venue in `execution/paper.py`. No credentials are read and nothing is signed.

```bash
tyrex-pm run --config config/runs/settlement_value_continuous_paper.yaml --validate-config
tyrex-pm run --config config/runs/settlement_value_continuous_paper.yaml --paper
# stop with Ctrl-C: BUYs close first, committed holds are kept, the worklist stays in the ledger
tyrex-pm settlement --config config/runs/settlement_value_continuous_paper.yaml status
```

Set `TYREX_POLYGON_RPC_URL` (read-only) so the resolution worker can prove payouts of the real markets; without it holdings stay unresolved and the unresolved caps pause entries (`SETTLEMENT_WORKER_UNAVAILABLE` in the run evidence).

**Check in the run folder:**

- `windows.jsonl`: one row per window (episodes, attempts, posts, ledger states, strategy phase, closed reason). Every boundary must appear; count the windows that were skipped, paused or missed, and why.
- Run evidence: `ENTRY_OUTCOME` (kind, reason, retry class), `ENTRIES_PAUSED_FOR_WINDOW`, `COMMITTED_HOLD_DETACHED`, `SETTLEMENT_RECONCILED`, `LATE_FILL_DETECTED` (must be absent), `WINDOW_BUDGET_RESTORED` after a restart.
- `settlement status`: daily (UTC) realized loss, open worst case, unredeemed count/cost, consecutive losses. Convert UTC to Paris time when reporting.
- The paper account `state_directory/paper_account.json` must reconcile with the ledger (collateral = initial − debits + sale proceeds + redeemed payouts).

**Pass conditions:** no duplicate exposure, no unexplained accounting divergence, limits enforced across a restart (stop and restart once during the soak), every boundary accounted for, rollover delay measured. Duration alone is not proof. Paper fills say nothing about real fill quality or profitability.

## 7. Continuous live (not activated)

`config/runs/settlement_value_continuous_live.template.yaml` deliberately fails validation until every `settlement.run_limits` value is replaced by an explicit, operator-chosen amount. Use it only after: the provider qualification items of the implementation plan (P8), observation, a passing paper soak, and supervised single-window trials (section 3) with reconciled accounting. Start with a bounded soak (`market.maximum_duration_s` set explicitly).

**Pauses and how they clear:** limits are recomputed from the durable ledger before every attempt, so they clear when their cause does (a holding resolves and is redeemed, a new UTC day starts while open risk stays counted). A consecutive-loss pause clears only with `tyrex-pm settlement --config <cfg> ack-loss-pause --reason "<why>"`. An uncertain entry (`DISPATCH_UNKNOWN`) or a late fill stops the runner with `MANUAL_INTERVENTION_REQUIRED`; resolve it as in section 5.

## 8. Managed exits and re-entry (paper / research only)

`config/runs/settlement_value_managed_paper.yaml` combines strategy exits (thesis stop, economic value exit), a protective stop-loss (`protection:`) and serial re-entry. All its numbers are hypotheses; research them in replay before any trial. Check `EXIT_OUTCOME`, `SETTLEMENT_EXIT_OUTCOME` (early-sale proceeds are estimated with the strategy fee curve until verified against venue fees), `EXIT_INTENT_DROPPED` (one exit owner; committed holds are never sold) and the per-episode rows of `windows.jsonl`.

## What has been validated, and what has not

| Validated by automated tests (fakes, recorded data) | Requires the runs above |
|---|---|
| Ledger buckets, idempotency, torn-tail recovery | Real fee-schedule read from Gamma |
| Budget includes pending, unknown and unresolved positions, plus the proposed order | Real fills and fee charging |
| Constraint gate blocks on unknown or mismatched fees, tick or minimum | Hold commitment on a live session |
| Hold commitment, flatten/shutdown exemption, hold-aware startup | Finalized payout read for a real position |
| Payout proof and redemption attribution logic (synthetic receipts) | Receipt verification of a real redemption |
| Observation is order-free (import closure) | Live signal frequency, latency, RTDS vs PolyBolt identity |
| Entry retry rules, durable attempt/POST budgets, no duplicate BUY on a POST timeout, late-fill detection (fake venue) | Real retry frequency, recovery latency and venue error codes |
| Continuous detach of committed holds, run limits, resolution worker, paper account (fakes) | A continuous paper soak on live public feeds (section 6) |
| Early-sale ledger accounting, exit arbitration, re-entry gating (fakes, recorded books) | Real SELL fills, sell fees, exit reliability near close |
