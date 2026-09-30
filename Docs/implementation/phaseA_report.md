# Phase A report — current release evidence

Date: 30 September 2026. This pass closes A1, A2, A3, and A5. It does not run A4 and does not submit an order.

The candidate parent is `1d3a575` (`fix: classify and avoid cutoff-edge entry intents`). The release checker records the candidate commit after this report is committed. `scripts/check_release_candidate.py` is allowed to be the only dirty file, because it must name that commit.

## A1 — Reliability fixes

No new trading behavior was added in this pass. The inspection, checkpoint, settlement, and timing changes were already in the working tree. This task records what they guarantee and what the 27 September live run actually showed.

### What the code enforces

- Background inspection uses a second SDK client. It does not take the execution client's slot. Account refresh still uses the execution client.
- An activity checkpoint is written only after the inspection is accepted: coverage is complete, opening and closing holdings and orders agree, and no other execution event landed during publication. Evidence is appended before the checkpoint. A discarded or withheld inspection leaves the checkpoint where it was.
- A Polygon `latest` payout is read again on every inspection. Process lifetime does not make it final. The cache returns a proof only when finality, block identity, and the token mapping are all present. The current adapter cannot produce that proof, so clearance uses the fresh read. Neg-risk stays unresolved.
- Prepare timings merge by presence. A missing value does not erase a measured zero.

### What the 27 September run showed

Run `var/runs/ask70/r7_1d3a575_20260927T174900Z`, config `config/runs/ask70_protection_tiny_live.yaml`.

- Startup inspection was a full activity read: 3 pages, 5641 ms, publication `accepted`, checkpoint `advanced`, ready true.
- Later incremental inspections were 1 page and about 3.5–6.2 seconds, also accepted.
- The buy prepared with `prepare_queue_wait_ms` 0, `refresh_wait_ms` 0, and `create_market_order_ms` 15. The post round trip was 391 ms. Inspection did not hold that preparation.
- The inspection that overlapped the stop sell was `discarded`, evidence `incomplete`, checkpoint `unchanged`.
- The first take-profit waited 500 ms on an execution-client refresh, did not sign, and kept those timings. The retry posted.

### What remains offline-only

- A changed or failed payout is covered by `tests/unit/execution/test_settlement_finality.py`. No resolved market was on the live account, so revalidation was not observed live.
- A truncated order read and an opening/closing mismatch are covered by `tests/unit/execution/test_inspection_isolation.py`. The live discarded inspection was caused by the sell changing execution, not by a partial order page.

## A2 — Offline baseline

Run on 30 September 2026 with the repository `.venv`.

| Check | Result |
| --- | --- |
| `pytest -q` | 468 passed |
| `z_gap_tiny_live.yaml` | config ok |
| `ask70_protection_tiny_live.yaml` | config ok. Hard cutoff 105 seconds, soft cutoff 106 |
| `ask70_maker_tiny_live_danger.yaml` | config ok |
| `feature_probe_catalog.yaml` | config ok |
| `ask70_protection_tau30_experimental.yaml` | config ok. Hard cutoff 30 seconds, soft cutoff 31. Not the A4 profile |
| `z_gap_entry_eligible.json` | ENTER / ENTRY_CANDIDATE |
| `z_gap_no_entry_eligible.json` | SKIP / NO_ECONOMIC_EDGE |
| `z_gap_inputs_invalid.json` | WAIT / PTB_NOT_USABLE |
| `z_gap_exit_held.json` | EXIT / MARKET_RICH_EXIT at τ=180 |

The earlier 233-test run was a subset. This table is the full offline gate for this candidate.

`scripts/check_release_candidate.py` previously required commit `80aeca2`. That is no longer the candidate. After this report's commit, the script names the new commit and still refuses any other dirty path.

## A3 — Account and previous sessions

Source: `var/runtime_state/execution/execution.sqlite3`, read on 30 September 2026. The journal was not deleted and was not rewritten by this pass. Event count stayed 45.

The journal meta binds the account on chain 137, environment `production`. The activity ledger is schema 1, 1197 records, `incremental_since_full` 4. Its coverage timestamp is 2026-09-08T19:04:00Z. That is the newest stored activity row, not the 27 September trades. The inspection during the sell was discarded, so it did not move this checkpoint. The next accepted inspection can pick up later activity from the overlap window.

Three sessions are in the journal:

| Session suffix | What the journal shows |
| --- | --- |
| `591427c05421` | 27 September 12:22 UTC. Entry reached `FINAL_GATE_REJECTED` before any submission. Terminal no-dispatch. No position. |
| `123fdb94a2f9` | 27 September 13:26 UTC. Same pre-POST final-gate rejection. Terminal no-dispatch. No position. |
| `81d07ee64a0d` | 27 September 17:50 UTC. Buy 6.712329 at 0.73 reached `CONFIRMED`. Resting take-profit posted, then cancelled. Sell 6.71 at 0.58 is `MATCHED` only. Dust 0.002329 recorded. No later balance that shows the sale. |

The dust phase is terminal inside the bot, and a dust phase does not count as exposure even while `confirmed_position_shares` is still 6.712329. That is a journal classification, not a venue balance.

The older copied-state blockers (`LEGACY_JOURNAL_UNBOUND`, an unexpected holding, and unresolved attempts) are not the current journal. This journal has a wallet binding, and the two failed entries have no submission attempt left open.

The online re-read did not complete. This command was used:

```text
python -m tyrex_pm.application.cli run --check-account --config config/runs/ask70_protection_tiny_live.yaml --dotenv .env --out-dir var/runs/ask70/phaseA_account_check
```

It stopped with `TransportError: certificate verify failed: Hostname mismatch` for `clob.polymarket.com` before an account read. No order was sent. The empty directory `var/runs/ask70/phaseA_account_check` has no report. TLS verification was not disabled.

Before A4, run `--check-account` from the operator environment that reached the venue on 27 September. Continue to `--live` only if that report says the account is ready.

## A5 — Capability record for this candidate

Required, pending, and excluded follow `Docs/implementation/first_release/release_validation_campaign.md`. "Observed" means the event happened. It is not a pass where the campaign requires a confirmed result.

| Capability | Class | Evidence |
| --- | --- | --- |
| Offline pytest, config validation, lab packs | Passed | A2 table above |
| Live report shape (schema 5, timeline, fills, startup block) | Passed for the 27 September report | `r7_1d3a575_20260927T174900Z/run_summary.json` |
| One-shot interrupt while the runtime is active | Pending | Not exercised in the reviewed runs |
| Safe no-entry before a session | Observed earlier | Cutoff block, `ok: true`, no mutation. Not re-scored as the filled-round-trip row |
| Submission that ends unfilled | Not this row | The two final-gate failures never posted. They are local rejections, not `COMPLETED_NO_FILL` after a POST |
| Confirmed buy and confirmed exit | Pending | Buy confirmed. Sell matched only. `observed_fills_complete` false |
| Position closed after a live entry | Observed, not confirmed | Take-profit posted, then cancelled. Stop sell matched. Dust recorded. Balance agreement missing |
| Resting take-profit fill | Pending, not required | The 0.85 order was cancelled for the stop |
| Stop-loss sell on this candidate | Observed matched | 6.71 at 0.58. Not chain-confirmed |
| Maker buy | Excluded | Different YAML. Not required for this release |
| Feed reconnect qualification | Not passed | Crossed books on the traded tokens resynced, and the buy had no `BOOK_DESYNC` blocker. That is not a reconnect test |
| F-L2 | Excluded | Deferred |
| R6 continuous windows | Excluded | Deferred |
| Negative-risk settlement | Unresolved | Current policy. Not a BTC binary prerequisite |

## What A4 still has to prove

One bounded live process on this candidate, using `config/runs/ask70_protection_tiny_live.yaml`, after a ready `--check-account`.

The missing proof is a sell that reaches `CONFIRMED` and a balance read that agrees, so `observed_fills_complete` can be true.

The current host sets `stop_requested` when an exit returns a terminal phase. Dust is recorded as soon as the unsellable remainder is classified. On 27 September that was about one second after the sell matched, while the buy had taken about six seconds to move from matched to confirmed. Waiting at the console will not keep that process alive. Another run on this same host can stop at the same point. A4 can still show the startup assessment, the entry, and the exit path. It should not be treated as a confirmed round trip unless the new report says the sell is confirmed and the balance agrees.

## Operator commands

Check the candidate, then the account, then the live process. Do not add `--live` to the account check.

```text
python scripts/check_release_candidate.py --config config/runs/ask70_protection_tiny_live.yaml
```

```text
./.venv/Scripts/python.exe -m tyrex_pm.application.cli run --check-account --config config/runs/ask70_protection_tiny_live.yaml --dotenv .env --out-dir var/runs/ask70/phaseA_account_check_$(date -u +%Y%m%dT%H%M%SZ)
```

Run the live command only if the account check prints a ready account:

```text
./.venv/Scripts/python.exe -m tyrex_pm.application.cli run --config config/runs/ask70_protection_tiny_live.yaml --dotenv .env --live --out-dir var/runs/ask70/r7_$(git rev-parse --short HEAD)_$(date -u +%Y%m%dT%H%M%SZ)
```

The 30-second file is not this command. R6, automatic entry retries, and F-L2 stay deferred.
