# Tiny-LIVE Readiness Packet

**Date:** 2026-08-08  
**Status:** Ready for **owner-operated** tiny-LIVE only — agents must **not** run `--live`.  
**Implementation outcome:** COMPLETE (Phases 1–7 + 10).  
**SDK pin:** `polymarket-client==0.5.0`  
**Deterministic suite:** **1076 passed** (`full_suite_result.txt`).

## Preconditions

1. Working tree reviewed; full deterministic suite green (`Docs/implementation/runtim_enhancement/full_suite_result.txt`).
2. `.env` contains authenticated credentials (private key + L2 API creds + funder as documented in `.env.example`).
3. **Collateral USDC allowance** and **conditional-token / ERC-1155 operator allowance** for both UP and DOWN tokens must already be sufficient for the $5 fee-inclusive path. Tyrex **will not** auto-approve.
4. No unresolved blocking lifecycle sessions under `var/runtime_state/n7/` (or accept MANUAL handoff). Unscoped blocking records fail closed to MANUAL.
5. Confirm `TYREX_ALLOW_LAZY_LIVE_INIT` is **unset**.
6. Read `RUNTIME_ENHANCEMENT_IMPLEMENTATION_REPORT.md` before arming.

### Allowance remediation (official / operator)

If readiness reports `INSUFFICIENT_COLLATERAL_ALLOWANCE` or `INSUFFICIENT_CONDITIONAL_ALLOWANCE`:

- Use Polymarket account / official SDK wallet tooling **outside** the Tyrex LIVE hot path to set CLOB collateral allowance and conditional token operator approvals for the exchange contracts used by your wallet type (EOA / proxy / Safe as applicable).
- Re-run authenticated readonly preflight before `--live`.

## Exact owner Git Bash command

From the repository root (`Tyrex_PM`):

```bash
cd /c/Users/elies.gherbi/Desktop/work/pm/Tyrex_PM

python -m tyrex_pm.application.cli run \
  --mode live \
  --live \
  --strategy config/strategies/z_gap.yaml \
  --risk config/risk/tiny_live_5usd.yaml \
  --execution config/execution/polymarket_live.yaml \
  --runtime config/runtime/live_btc_5m.yaml \
  --dotenv .env \
  --reporting config/reporting/full.yaml \
  --max-duration-s 300 \
  --out-dir var/runs/z_gap/tiny_live_post_enhancement_$(date -u +%Y%m%dT%H%M%SZ)
```

Equivalent N7 tool path:

```bash
cd /c/Users/elies.gherbi/Desktop/work/pm/Tyrex_PM

python tools/n7_live/run_n7_live_oneshot.py \
  --live \
  --strategy config/strategies/z_gap.yaml \
  --risk config/risk/tiny_live_5usd.yaml \
  --execution config/execution/polymarket_live.yaml \
  --runtime config/runtime/live_btc_5m.yaml \
  --dotenv .env \
  --reporting config/reporting/full.yaml \
  --max-duration-s 300
```

Readonly preflight (no mutations):

```bash
python tools/n7_live/run_n7_readonly_preflight.py \
  --dotenv .env \
  --strategy config/strategies/z_gap.yaml \
  --risk config/risk/tiny_live_5usd.yaml \
  --execution config/execution/polymarket_live.yaml \
  --runtime config/runtime/live_btc_5m.yaml
```

## Expected artifacts

Under `var/runs/z_gap/<run>/`:

- `audit_events.jsonl`
- `analytics_events.jsonl`
- `run_summary.json`
- `manifest.json`
- `attachments/operator_outcome.json`
- `compose/summary.json`
- `effective_n7_sealed.json` (when materialized)

Under `var/runtime_state/n7/<run_id>/`:

- `lifecycle_state.json` (authoritative crash recovery)

## Post-run inspection

```bash
RUN=var/runs/z_gap/<run_dir>

rg -n "n7_user_stream_ready|command_submit_dispatch|mutation.venue_submit_result|PASS_N7_NO_FILL|http_post_started|execution_obligation_resolved_no_fill" \
  "$RUN/audit_events.jsonl"

python - <<'PY'
import json
from pathlib import Path
p = Path("var/runs/z_gap/<run_dir>/attachments/operator_outcome.json")
print(json.dumps(json.loads(p.read_text()), indent=2)[:4000])
PY
```

Verify:

- preparation / user stream ready **before** candidate
- `candidate_selected_to_http_post_started_ms` ≪ 33500
- certain FAK no-fill → `PASS_N7_NO_FILL`, `ok=true`, `venue_acceptances=0`
- no YES-token fallback in recovery scope
- `operator_live_armed_ever` vs `_at_end` counters coherent

## Agent confirmation

This packet does **not** authorize the implementation agent to execute `--live`.
