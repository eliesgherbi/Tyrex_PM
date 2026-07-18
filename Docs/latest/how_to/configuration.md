# Configuration

**Purpose:** where settings come from and how they interact.  
**Never** copy values from a real `.env`.

## Sources and precedence

1. CLI flags (highest for that run)
2. JSON config file (`--config`)
3. Process environment / `.env` (credentials and address roles)
4. Code defaults / policy modules (e.g. `r7_lifecycle_policy`)

Committed seals:

- `config/r7/acknowledgment_policy.json` — sealed acknowledgment identities (R7 phase-specific)

Example JSON configs (all exist in-repo):

| File | Typical use |
|------|-------------|
| `config/observe_fixture_r3.json` | Offline observe |
| `config/observe_live_r3.json` | Public live observe |
| `config/observe_shadow_r5.json` | Shadow OMS |
| `config/observe_shadow_r4.json` | Older shadow sample |

## Environment variables (names only)

Loader: `tyrex_pm.execution.polymarket.auth`.

| Name | Role | Precedence / notes |
|------|------|--------------------|
| `TYREX_PRIVATE_KEY` | Signer private key | Preferred over `POLYMARKET_PK` |
| `POLYMARKET_PK` | Signer private key | Deprecated alias of the above |
| `TYREX_FUNDER` | Funder / proxy wallet | Preferred over `POLYMARKET_FUNDER` |
| `POLYMARKET_FUNDER` | Funder / proxy | Deprecated alias |
| `TYREX_SIGNATURE_TYPE` | `0` EOA, `1` proxy, `2` safe, `3` 1271 | Preferred over `POLYMARKET_SIGNATURE_TYPE` |
| `POLYMARKET_SIGNATURE_TYPE` | Same | Deprecated alias |
| `POLYMARKET_API_KEY` | L2 key | Required for authenticated paths |
| `POLYMARKET_API_SECRET` | L2 secret | Required |
| `POLYMARKET_PASSPHRASE` | L2 passphrase | Or `POLYMARKET_API_PASSPHRASE` |
| `POLYMARKET_API_PASSPHRASE` | Passphrase alias | Alternate name |
| `POLYMARKET_ADDRESS` | Optional **signer** override | Synthetic/tests only when no PK; **never funder** |

**Important:** `POLY_ADDRESS` is the **HTTP header** set to the signer EOA derived from the private key. It is not a preferred `.env` knob. Pre-R6D confusion (putting funder in `POLY_ADDRESS`) caused authenticated L2 failures — do not repeat.

Public fixture observe/shadow do not require these.

## Common JSON fields

| Name | Type | Meaning | Safety |
|------|------|---------|--------|
| `mode` | string | `fixture` / `live` | fixture = offline |
| `fixture_path` | path | recorded events | — |
| `output_path` | path | facts JSONL | report write |
| `binance_symbol` | string | reference symbol | ref-only |
| `momentum_*` | num | validation strategy params | not alpha |
| `freshness.*` | ms / basis | staleness gates | fail-closed |
| `risk.*` | mixed | notional/spread/kill switch | risk |
| `shadow.*` | mixed | ShadowOMS limits / persistence path | paper; may local-state write |

## Runtime flags (CLI)

| Flag | Command | Meaning |
|------|---------|---------|
| `--config` | observe/shadow | JSON path |
| `--mode` | observe | override fixture/live |
| `--btc-window` | observe/shadow | `current` / `next` |
| `--which` | `discover-btc-window` | `current` / `next` |
| `--duration-s` | observe/shadow | live runtime seconds |
| `--output` | several | report path |
| `--dry-run` | `r7b-live-once` | no venue mutation (default when not executing) |
| `--execute-live` | `r7b-live-once` | venue mutation (operator only) |
| `--skip-auth` | `live-preflight` | public connectivity only |

## Paths

```text
var/state/       local persistent operational state (gitignored)
var/reporting/   runtime-disposable evidence
config/          committed configs + sealed ack policy
```

## Secret-handling rules

- Never commit `.env`
- Never log secrets or full addresses
- Prefer suffixes in reports
- Do not paste credentials into documentation
