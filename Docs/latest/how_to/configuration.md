# Configuration

**Purpose:** where settings come from and how they interact.  
**Never** copy values from a real `.env`.

## Sources and precedence

1. CLI flags (highest for that run)
2. JSON config file (`--config`)
3. Process environment / `.env` (credentials and address roles)
4. Code defaults / policy modules (e.g. `r7_lifecycle_policy`)

Committed seals:

- `config/r7/acknowledgment_policy.json` — exact four ack identities

Example JSON configs:

| File | Typical use |
|------|-------------|
| `config/observe_fixture_r3.json` | Offline observe |
| `config/observe_live_r3.json` | Public live observe |
| `config/observe_shadow_r5.json` | Shadow OMS |
| `config/observe_shadow_r4.json` | Older shadow sample |

## `.env.example` (names only)

| Name | Meaning | Modes |
|------|---------|-------|
| `TYREX_PRIVATE_KEY` / `POLYMARKET_PK` | Signer key | auth / live |
| `TYREX_FUNDER` / `POLYMARKET_FUNDER` | Funder/proxy | proxy modes |
| `TYREX_SIGNATURE_TYPE` / `POLYMARKET_SIGNATURE_TYPE` | 0 EOA, 1 proxy, 2 safe, 3 1271 | auth / live |
| `POLYMARKET_API_KEY` / `SECRET` / `PASSPHRASE` | L2 triple | auth / live |
| `POLYMARKET_ADDRESS` | Optional signer override (tests) | special |

Public fixture observe/shadow do not require these.

## Common JSON fields

| Name | Type | Meaning | Safety |
|------|------|---------|--------|
| `mode` | string | `fixture` / `live` | fixture = offline |
| `fixture_path` | path | recorded events | — |
| `output_path` | path | facts JSONL | disposable |
| `binance_symbol` | string | reference symbol | ref-only |
| `momentum_*` | num | validation strategy params | not alpha |
| `freshness.*` | ms / basis | staleness gates | fail-closed |
| `risk.*` | mixed | notional/spread/kill switch | risk |
| `shadow.*` | mixed | ShadowOMS limits / persistence | paper only |

## Runtime flags (CLI)

| Flag | Command | Meaning |
|------|---------|---------|
| `--config` | observe/shadow | JSON path |
| `--mode` | observe | override fixture/live |
| `--btc-window` | observe/shadow | `current` / `next` |
| `--duration-s` | observe/shadow | live runtime seconds |
| `--output` | several | report/facts path |
| `--dry-run` | `r7b-live-once` | read-only (default when not executing) |
| `--execute-live` | `r7b-live-once` | mutation-capable (operator only) |

## Paths

```text
var/state/       required operational state
var/reporting/   disposable evidence
config/          committed configs + sealed ack policy
```

## Secret-handling rules

- Never commit `.env`
- Never log secrets or full addresses
- Prefer suffixes in reports
- Do not paste credentials into documentation
