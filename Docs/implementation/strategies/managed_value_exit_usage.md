# Using the experimental managed value exit

The entry model and entry parameters do not change when this exit is enabled.
This is an optional rule for a position already owned. It works through the same
strategy implementation in paper/live execution and research replay.

## Start with a complete paper profile

Two complete examples already validate:

- `config/runs/settlement_value_leader_5c_240s_e1_value_paper.yaml`: value-only exit.
- `config/runs/settlement_value_leader_5c_240s_e2_value_trend_paper.yaml`: value exit
  with downward evaluated-bid trend confirmation.

They retain comparison + legacy entry settings: 5-cent conservative edge,
30–240 seconds remaining, intended debit cap $5, no entry retry and no re-entry.
Both are paper-only, with separate account/ledger/state paths.

```powershell
.venv/Scripts/tyrex-pm.exe run --config config/runs/settlement_value_leader_5c_240s_e2_value_trend_paper.yaml --validate-config
.venv/Scripts/tyrex-pm.exe run --config config/runs/settlement_value_leader_5c_240s_e2_value_trend_paper.yaml --paper
```

Validation needs no network. Paper uses public feeds and simulated holdings; it sends
no real exchange orders. Use a copied profile with a new run name and separate state
paths for a separate experiment. Qualification runs use isolated profiles under
`var/research/trading_integrity`, not these reusable examples.

## The exit block

This is an overlay on a complete run configuration, not a standalone file:

```yaml
strategy:
  parameters:
    exit:
      policy: managed_exit
      value_exit_enabled: true
      value_exit_basis: point
      value_exit_margin_per_share: '0.02'
      value_exit_trend_enabled: true
      value_exit_trend_policy: evaluated_bid_trend_v1
      value_exit_trend_lookback_s: 5
      value_exit_trend_min_drop: '0.02'
      value_exit_trend_confirmation_s: 2
      value_exit_trend_max_gap_s: 2
      exit_cutoff_tau_s: 30
      residual_policy: hold_to_resolution
      thesis_stop_enabled: false

lifecycle:
  mode: managed_exit
```

Keep the other lifecycle, settlement, risk and account fields from the complete
profile. In these profiles the strategy cutoff is 30 seconds, before the host's
20-second mandatory-exit threshold and 5-second manual deadline. Configuration
validation enforces the compatible ordering. Do not use the strategy's older
15-second default cutoff with this run template.

- `policy` and `lifecycle.mode` select managed ownership **before entry**. An already
  committed hold is not retroactively converted into a sellable managed position.
- `value_exit_enabled` turns on the economic rule.
- `value_exit_basis: point` uses the held token's point probability. The existing
  `upper` alternative requires the bid to beat the model's optimistic bound instead.
- `value_exit_margin_per_share: '0.02'` requires 2 cents of fee-net sale value above
  the probability-based hold value. It is not a 2% stop-loss or a drop from entry.
- `value_exit_trend_enabled: false` gives E1, value-only. `true` gives E2, requiring
  both the economic condition and the following trend condition.
- `lookback_s: 5` compares the current evaluated best bid with an eligible earlier
  bid around five seconds ago. It uses past observations only.
- `min_drop: '0.02'` requires that bid to have fallen by at least 2 cents.
- `confirmation_s: 2` requires that decline condition to persist for two seconds.
- `max_gap_s: 2` resets confirmation when observations are more than two seconds
  apart. Unready books and connection-epoch changes reset it too.
- `exit_cutoff_tau_s: 30` stops new early exits at the cutoff. Proven remaining
  inventory follows `residual_policy: hold_to_resolution`; uncertainty is reconciled
  or escalated, not disguised as a successful hold.
- `thesis_stop_enabled: false` keeps the separate probability-floor stop disabled.

The numerical settings above are provisional hypotheses. Software qualification
does not establish that they improve investment returns.

## What happens step by step

1. Entry uses the existing conservative probability and entry rules.
2. After a fill, the strategy continues evaluating the **held side**. For UP,
   `p_hold = p_up`; for DOWN, `p_hold = 1 - p_up`. It does not switch which side it
   manages merely because the other side is now more likely to win.
3. It walks executable bid depth and subtracts estimated selling fees. Eligible
   depth must provide at least `p_hold + margin` per share. Only eligible size can
   be requested; the system may make a partial sale.
4. With the trend option on, the bid-decline condition must also be confirmed.
5. Before submission, the host repeats readiness, economic and trend checks. An
   EXIT request is not a fill; an uncertain or partially filled SELL remains tracked.
6. At cutoff, any proven residual position is committed to settlement. One owner
   controls exit orders, preventing competing sells.

Example: held-side probability 0.55, fee-net executable bid 0.60, margin 0.02.
The economic test passes because 0.60 >= 0.57. E1 may request a sale; E2 additionally
needs the confirmed downward bid trend.

Counterexample: probability 0.9995 while the token falls to 0.12. The value rule does
not sell, because selling is still worse according to the model. This rule cannot
independently detect model overconfidence and is not a price-based stop-loss.

An early sale does not enable another entry by itself. Entry retry and re-entry are
separate settings and remain disabled in these profiles. To restore the exact frozen
hold behavior, use the original hold profile: both policy/mode are hold-to-resolution
and the value/trend rules are disabled.

## Inspecting evidence

Notebook 06, section 9, opens the saved **Exit cases (experimental)** dataset. Compare
E0 hold, E1 value and E2 value+trend on the same scenario/window. Its saved timeline
separates entry requests, exit requests and confirmed SELL fills; the exit-context
table shows probability, executable-exit facts and recorded trend evidence.

Optional diagnostic recording:

```yaml
recording:
  level: explain
  explain_trigger: all
  raw_inputs: true
  raw_on_fault: stop_entries
```

Use full raw capture for bounded diagnosis/reconstruction. It is not required for the
exit to operate. The recorder uses bounded background queues. The bounded loop screens
and their limits are in [the status report](trading_integrity_and_managed_exit_status_2026-10-20.md);
order-path overhead remains unqualified. `stop_entries` blocks new entries on a
capture failure while retaining management of existing positions.

```powershell
.venv/Scripts/tyrex-pm.exe recording reconstruct --capture <run-folder> --mode trading --out <diagnostic-folder>
```

The reconstruction command is offline. Older runs that lack execution-context capture
cannot be made reconstructable retrospectively.
