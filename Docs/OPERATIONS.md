# Operations — guru follow (v1)

## Config files

| File | Use |
|------|-----|
| `.env` | **Secrets only:** `POLYMARKET_PK`, `POLYMARKET_FUNDER`, `POLYMARKET_SIGNATURE_TYPE`, L2 API trio. Never commit. |
| `config/strategy/*.yaml` | Guru wallet, allowlisted token ids, `copy_scale`, optional strategy dedup path. |
| `config/risk/*.yaml` | Limits, kill switch, fail-closed notional rules. |
| `config/runtime/*.yaml` | `trader_id`, **`execution_mode`** (`shadow` / `live`), polling, logging, CLOB host/chain. |

Field-level reference: [`Docs/CONFIG_MODEL.md`](CONFIG_MODEL.md).

Starter files in-repo (replace guru wallet and token ids before relying on them):

- `config/strategy/guru_follow.yaml`
- `config/risk/guru_follow_risk.yaml`
- `config/runtime/live_polymarket.yaml`

## Run (after `pip install -e .`)

From repo root:

```bash
python scripts/run_guru.py ^
  --strategy-conf config/strategy/guru_follow.yaml ^
  --risk-conf config/risk/guru_follow_risk.yaml ^
  --live-conf config/runtime/live_polymarket.yaml
```

(Unix: line continuation with `\`.)

Optional: `TYREX_PM_DOTENV=/path/to/.env` to load a non-default env file.

## Modes

| `execution_mode` | Behavior |
|------------------|----------|
| **`shadow`** | **`ConfiguredRiskPolicy`** is active. Intents go to **`NoOpExecutionPort`** — **no CLOB orders**. Logs `shadow_order_intent`. |
| **`live`** | Same strategy + risk. **`PolymarketExecutionPolicy`** signs and posts LIMIT orders via py-clob. Logs `live_order_intent` from strategy; `event=live_order_submit` / `live_order_error` from execution. |

## Before live

1. Complete auth verification: `python scripts/verify_polymarket_auth.py`.
2. Supervised order smoke: `examples/order_lifecycle_smoke.py` and `Docs/Runbooks/order_lifecycle_v1_02.md`.
3. Set conservative **`risk`** YAML (`max_*`, `kill_switch` test).
4. Set `execution_mode: live` only in **runtime** YAML — strategy and risk files unchanged.
5. Confirm allowlist token ids match resolution / CLOB `asset` strings.
6. Ensure `var/` (dedup state) is writable.

## Environment variables (non-secret / tooling)

- `TYREX_MIN_BUY_NOTIONAL_USD` — minimum BUY notional guard in live execution (default `1`).
- Smoke / tooling vars: `Docs/Runbooks/order_lifecycle_v1_02.md`, `examples/order_lifecycle_smoke.py`.

## Logs to grep

| `event=` | Meaning |
|----------|---------|
| `guru_signal_emitted` | New deduped guru trade from `GuruMonitorActor` |
| `guru_poll_tick` | Poll cycle / backoff |
| `copy_skip` | Strategy dropped signal (allowlist, zero qty, risk denied, …) |
| `shadow_order_intent` | Shadow mode: intent reached execution port (no venue I/O) |
| `live_order_intent` | Live mode: strategy forwarded intent to execution policy |
| `live_order_submit` | CLOB accepted the posted order (payload prefix in log) |
| `live_order_error` | CLOB / policy rejection or exception |
| `strategy_started` | Strategy boot |

Risk denials appear on `copy_skip` with `reason_code=risk_denied` and the policy reason string.

## Troubleshooting

- **No `shadow_order_intent` / `live_order_intent`:** check allowlist vs guru `asset`; watch `copy_skip` (`not_allowlisted`, `zero_qty`, `risk_denied`, …).
- **Guru duplicates:** dedup store (`guru_dedup_state_path`); delete file for full replay in dev only.
- **Live immediate rejects:** check `live_order_error`, min BUY notional, tick size, and balance; run order-lifecycle smoke first.
- **Config validation errors:** messages cite the YAML path and field; see `CONFIG_MODEL.md`.
