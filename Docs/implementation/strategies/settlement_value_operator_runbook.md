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

## What has been validated, and what has not

| Validated by automated tests (fakes, recorded data) | Requires the runs above |
|---|---|
| Ledger buckets, idempotency, torn-tail recovery | Real fee-schedule read from Gamma |
| Budget includes pending, unknown and unresolved positions, plus the proposed order | Real fills and fee charging |
| Constraint gate blocks on unknown or mismatched fees, tick or minimum | Hold commitment on a live session |
| Hold commitment, flatten/shutdown exemption, hold-aware startup | Finalized payout read for a real position |
| Payout proof and redemption attribution logic (synthetic receipts) | Receipt verification of a real redemption |
| Observation is order-free (import closure) | Live signal frequency, latency, RTDS vs PolyBolt identity |
