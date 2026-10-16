# Frozen hold candidates: continuous operation after v2 A/B/C

Status: profiles prepared and checked against the implementation checkout's v3 config
loader. No feeds, orders, paper soak, merge or new experiment were started by this work.
The completed v2 experiment remains the selection evidence, not a live qualification.

## Profiles

All four profiles use TWAP proxy diffusion with a 60-second averaging window, a 30-second
last-entry cutoff, a 0.97 maximum entry price, one tick of limit buffer, and the frozen
volatility, quality and sizing parameters (including 250 ms clock uncertainty).
The intended debit target is $5 with the runtime's fee reserve and final execution gates.
There is exactly one authorized entry attempt per window, at most one POST, no top-up,
no strategy retry, no managed exit, no protection block and no re-entry. A committed
holding survives shutdown and stays until resolution. Existing pre-commit recovery
remains part of the runtime; choosing hold does not remove failure recovery.

- Default: `config/runs/settlement_value_baseline_2c_150s_live.template.yaml`
  and `config/runs/settlement_value_baseline_2c_150s_paper.yaml`.
  Minimum conservative edge $0.02/share; entry with 30–150 seconds remaining.
- Comparison: `config/runs/settlement_value_leader_5c_240s_live.template.yaml`
  and `config/runs/settlement_value_leader_5c_240s_paper.yaml`.
  Minimum conservative edge $0.05/share; entry with 30–240 seconds remaining.

`account.retry_attempts` retries account reads, not entry orders. The explicitly disabled
`strategy.parameters.retry` and its caps control the trading attempts.

Live templates intentionally fail validation while `REQUIRED` values remain. Fill in
the trial budgets, account-wide run limits and redemption wallet only when the allocated
capital and acceptable loss have been chosen. No real-money budget was inferred from
the backtest. Initial live operation is continuous across windows but bounded to 1800
seconds. After qualification, removing `market.maximum_duration_s` allows operation
until manually stopped, subject to risk, cash and operator-intervention stops.

Paper balances and limits are simulated examples. Each paper candidate has its own
simulated account and ledger. Simulated automatic redemption does not exist in live:
real resolution tracking is automatic, while redemption remains an operator task.

Live candidates share a settlement ledger and execution state directory. Run only one
at a time against an account, keep the same working directory and retain durable state
when switching parameters. Shared files are not authorization for concurrent runners.
When later moving from the implementation checkout to the main checkout, transfer and
reconcile any live state before starting there; relative paths otherwise create a new
ledger in the new working directory. Do not discard a ledger to clear a limit.

The live settlement/reference identifiers follow the existing runtime template rather
than the research metadata labels. Fee provenance requires a provider match, while the
numerical fee curve remains the research assumption. Neither change verifies the real
settlement rule or fee charging. Confirm those before real orders.

## Order of work

1. Preserve the completed v2 notebook, frozen parameters and experiment outputs.
   No v2 grid rerun is needed merely to enable continuous operation.
2. Review and commit the implementation in `E:/polymarket/Tyrex_PM-live`; inspect the
   complete diff and resolve material review findings. The implementation must be
   committed before a Git merge can transfer it. Preserve the main checkout's current
   uncommitted baseline closeout files too; do not reset or overwrite them.
3. In that checkout, rerun just these two fixed candidates on the same 94 development
   windows using v3's `planner_amount` order contract and final gate, with retry off,
   hold and no re-entry. Save a new experiment. This attributes the execution change;
   it is not a new parameter search. Review per-window changes and shared-account
   simulation. The earlier 23-window regression is not this full candidate check.
4. Complete provider qualification (actual resolution rule, opening K, ties, reference
   feed, fee schedule/fee charging, order rounding and latency), with signal observation.
   Keep the existing holdout untouched until the fixed-policy evaluation is specified;
   evaluate once and report it separately from the development comparison.
5. Run each paper profile through continuous boundaries. Require at least 24 hours of
   operational evidence for the profile intended for live, including a restart,
   accounting reconciliation, risk-limit enforcement and no duplicate orders. Paper
   results validate operations, not real fill quality. A profile switch needs its own
   recorded checks; do not assume two profiles share identical operational evidence.
6. Merge the reviewed implementation between experiments, or continue qualification
   in the separate checkout first. Merge is a code-management step, not a live gate.
   Verify the executable imports the chosen checkout, and revalidate the profiles there.
7. Set the explicit live budgets, wallet, Polygon RPC and account credentials. Check
   the account, then conduct supervised, bounded trials with reconciled real fills,
   resolution and redemption. Start with one candidate, the provisional default.
8. After these checks pass, progress from the 30-minute continuous trial to longer
   supervised operation. Remove the duration only when ready for manual-stop operation.
   Limits or insufficient cash can pause new entries; manual redemption requires attention.

Retry and exit experiments remain later policy variants with new research evidence.
They do not need to be enabled to use continuous operation.

## Explicit checkout selection before merge (PowerShell)

The files prepared here are in the main checkout. The following explicitly selects the
new source while using the existing Python environment; it avoids an editable install
silently selecting the old source. All runtime relative paths then belong to `Tyrex_PM-live`.
Use a dedicated terminal and close it afterwards so the source override does not leak
into a later v2 notebook session.

```powershell
Set-Location E:\polymarket\Tyrex_PM-live
$env:PYTHONPATH = 'E:\polymarket\Tyrex_PM-live\src'
& E:\polymarket\Tyrex_PM\.venv\Scripts\python.exe -B -c "import tyrex_pm.runtime.run_config as c; print(c.__file__)"
& E:\polymarket\Tyrex_PM\.venv\Scripts\python.exe -B -m tyrex_pm.application.cli run --config E:\polymarket\Tyrex_PM\config\runs\settlement_value_baseline_2c_150s_paper.yaml --validate-config
```

To start the default's paper soak deliberately after the replay review:

```powershell
& E:\polymarket\Tyrex_PM\.venv\Scripts\python.exe -B -m tyrex_pm.application.cli run --config E:\polymarket\Tyrex_PM\config\runs\settlement_value_baseline_2c_150s_paper.yaml --paper
```

Use `settlement_value_leader_5c_240s_paper.yaml` for the comparison candidate. Ctrl-C
requests a clean stop. Save evidence and reconcile after restart.

After integration and explicit live-template completion, use the reviewed checkout's
CLI with `--validate-config`, then `--check-account`. The `--live` action is reserved
for the supervised trial after the preceding gates. This document does not launch it.

## Checks performed for these profiles

- Both paper configs loaded successfully with the actual v3 loader from `Tyrex_PM-live`.
- Both live templates failed validation as intended while budgets are `REQUIRED`.
- Temporary test-only budgets and a dummy wallet allowed the complete live structures
  to validate; those values were never saved into the templates.
- Numerical model, volatility, entry, sizing and quality values match the frozen v2
  JSON, except the intended edge and earliest-entry change for the leader.
- Every profile explicitly limits entry attempts/POSTs to one and selects hold only.

These are offline configuration checks. They do not replace replay evaluation, a paper
soak, code review or real provider qualification.
