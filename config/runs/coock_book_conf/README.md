# Step 2 paper verification configs

These files exercise the settlement lifecycle. They do not replace
`config/runs/settlement_value_leader_5c_240s_paper.yaml` and they are not a
new baseline. Real-money orders and on-chain transactions are out of scope.

Validate before any feed connects:

```text
python -m tyrex_pm.application.cli run --config config/runs/coock_book_conf/<file>.yaml --paper --validate-config
```

## Shared limits

A window counts when the run binds it as the active market. Each file allows
at most three windows: `market.maximum_duration_s: 900` is measured from the
first prepared window's start, so trading stops at the start of the fourth
window. The launcher also stops at 20 minutes of wall clock from the first
process start. That wall clock includes startup and, for S0-reach, the planned
restart. Whichever bound comes first ends the session. A session is not
extended if the target event does not occur.

State, ledger, and report paths contain `paper` and live under
`var/research/trading_integrity/step2/paper/<setup>/`.

## Files

| File | Purpose |
| --- | --- |
| `s0_reach_hold_paper.yaml` | Entry, fill, hold commitment, restart on the same account and ledger, rollover |
| `s1_retry_paper.yaml` | Same reach profile with bounded retry (3 attempts, 3 POSTs, 30s, 500ms gap) |
| `s2_managed_exit_paper.yaml` | Point-value exit, trend off, one trade, then residual hold |
| `s2_protection_paper.yaml` | That exit profile plus a 1% `stop_market_fak` stop. No take-profit or trailing stop |
| `s3_reentry_paper.yaml` | Managed exit plus a second episode after a confirmed flat sale |

## Deviations from the reference

- S0-reach sets `min_edge_per_share: 0.00` and `tau_max_s: 270`. The reference stays 0.05 and 240.
- Exit cutoff is 30 seconds, from the existing managed-exit paper profile. The host mandatory exit is 20 seconds, so a 15-second cutoff is invalid. Lifecycle mode is `managed_exit` on S2, S2-protect, and S3, and `hold_to_resolution` on S0 and S1.
- S3 sizes each episode at 2.40 USDC and caps the entry price at 0.45. Five shares, the venue minimum used here, then cost 2.25. Two such entries cost 4.50 and fit under the 5 USDC window cap. Sale proceeds do not restore that cap. S0–S2 keep the 5 USDC per-episode size because they only need one entry.
- Retry pass conditions use the configured ceiling of three attempts and three POSTs, including the first. S1 is the only file with retry enabled.
- Account transport `retry_attempts: 3` is not strategy retry.

## Commands

```text
python -m tyrex_pm.application.cli run --config config/runs/coock_book_conf/s0_reach_hold_paper.yaml --paper
```

The bounded launcher is `var/research/trading_integrity/step2/paper/run_bounded.py`.
S0-reach restarts once, after `HOLD_COMMITTED` is in its ledger, inside the same
20-minute wall clock. The other setups do not restart.

The 20 Oct 2026 sessions are under
`var/research/trading_integrity/step2/paper/`. S0-reach filled, committed,
restarted with the shares, and rolled over. Retry, the managed-exit posts,
the protection stop, and the re-entry attempt each ran once. Several target
events did not occur. S1–S3 set `recording.raw_inputs: true` so those
captures can be reconstructed. Trading parameters were not changed.
The result record is
`Docs/implementation/v_1_goal/02_execution_and_research_live_gaps.md`.

Evidence for each setup:

- `execution/paper_account.json` and `execution/execution.sqlite3`
- `settlement.jsonl`
- `reports/` for `run_summary.json`
- `recording/` for decision capture
