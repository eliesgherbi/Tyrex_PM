# Tyrex_PM

A Polymarket-native trading stack: small **event-driven runtime**, **venue adapters**, **explicit state stores**, **fail-closed risk**, and **structured reporting**. NautilusTrader is **not** the runtime spine.

```
guru / market data ──► signals ──► strategies ──► RiskEngine ──► OMS ──► CLOB
                                                       ▲                  │
                                                       └── state stores ◄─┘
                                                            (wallet, orders, marks)
                                                                      │
                                                                      ▼
                                                              facts.jsonl  (reporting)
```

## Quick start

```bash
pip install -e .[dev]            # development
pip install -e .[live]           # add live CLOB deps (py-clob-client-v2, websockets, dotenv)

# shadow run (no real submits, synthetic USDC bootstrap)
python -m tyrex_pm.runtime.app run --strategy config/strategies/guru_follow.yaml \
    --scenario shadow_guru --run-name first_shadow

# live run (requires .env with TYREX_PRIVATE_KEY [+ TYREX_FUNDER for proxy wallets])
tyrex-pm run --strategy config/strategies/guru_follow.yaml \
    --scenario live_guru --run-name first_live

# minimal end-to-end live attestation (post + cancel one tiny order)
tyrex-pm live-attest --token-id <numeric_clob_token_id> --size 1 --price 0.01 --side BUY
```

Each run writes `var/reporting/runs/<run_id_or_name>/{manifest.json,facts.jsonl,run_summary.json}`.

## Documentation

Start at **[`Docs/README.md`](Docs/README.md)**. Highlights:

| Audience | Read |
|----------|------|
| New to the repo | [`Docs/Architecture.md`](Docs/Architecture.md) |
| Operating a node | [`Docs/OPERATIONS.md`](Docs/OPERATIONS.md) |
| Changing the code | [`Docs/developer_guide.md`](Docs/developer_guide.md) · [`Docs/modules/README.md`](Docs/modules/README.md) |
| Tuning configuration | [`Docs/CONFIG_MODEL.md`](Docs/CONFIG_MODEL.md) |
| Reading `facts.jsonl` | [`Docs/reporting_fact_model.md`](Docs/reporting_fact_model.md) |
| Live truth & reconcile | [`Docs/LIVE_ARCHITECTURE.md`](Docs/LIVE_ARCHITECTURE.md) |

Secrets live in **`.env`** (never commit). See [`.env.example`](.env.example).

## Repository layout

```
src/tyrex_pm/
  core/           # events, models, ids, time, errors, reason codes
  ingestion/      # guru poll, market WS, user WS, historical backfill
  signals/        # reusable signal building blocks
  strategies/     # guru_follow (composition only)
  risk/           # RiskEngine + per-policy modules (fail-closed)
  execution/      # OMS (single-writer), order builder, lifecycle
  state/          # wallet/order/market/strategy stores + reconcile
  runtime/        # app entrypoint, config, supervisors, coordinator
  reporting/      # fact schema, sinks, summarizer
  venue/polymarket/   # CLOB bridge, WS, REST, normalizers, auth
config/
  risk/default.yaml             # global risk policy
  runtime/default.yaml          # supervisors, reporting, mode
  strategies/guru_follow.yaml   # strategy knobs
  scenarios/                    # shadow_guru, live_guru, live_attest
tests/                          # pytest suites (179+ cases)
Docs/                           # documentation root
```
