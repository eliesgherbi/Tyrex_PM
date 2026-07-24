# Configuration

**Purpose:** where settings come from and how they interact.  
**Never** copy values from a real `.env`.

## YAML run profiles (recommended for Z-Gap OBSERVE / SHADOW / LIVE)

Primary path for iterating Z-Gap without editing Python:

```text
config/strategies/   # strategy identity + ZGapConfig mathematics
config/risk/         # RiskPlanConfig (sole owner of target_notional; live_limits for LIVE)
config/execution/    # shadow OMS OR polymarket_live (N7)
config/runtime/      # source fixture|live, freshness, window/PTB / SSR policy
config/scenarios/    # one named leaf-level overlay
```

### Ownership

| Concern | YAML owner |
|---------|------------|
| Z-Gap math / entry / exit thresholds | `strategies/*.yaml` |
| Sizing & exposure (`target_notional`, risk `max_spread`) | `risk/*.yaml` |
| Tiny-live fee-inclusive caps / lineage | `risk/*.yaml` → `live_limits` |
| ShadowOMS / fills / retries | `execution/*.yaml` (`kind: shadow`) |
| Real Polymarket mutation mechanics | `execution/*.yaml` (`kind: polymarket_live`) |
| Data `source`, fixture, freshness, window, timers, SSR | `runtime/*.yaml` |
| OBSERVE / SHADOW / LIVE host | CLI `--mode` |
| Arm real mutations (LIVE only) | CLI `--live` |
| Run identity | CLI `--run-name` |

`source: live` selects market data. `--mode live` selects the N7 execution host.
They are not the same.

`signal_max_book_spread` (runtime → host signal gate) is **not** the same as risk `max_spread` (RiskEngine intent gate).

### Precedence

1. Typed code defaults  
2. Strategy + risk + execution + runtime YAML  
3. One `--scenario` leaf overlay (`config/scenarios/<name>.yaml`)  
4. Allowlisted CLI (`--mode`, `--run-name`, `--live` for LIVE)  
5. Hard validation (reject; never silent safety clamp)

### Commands

```bash
# Module form (works without installing the console script):
python -m tyrex_pm.application.cli run \
  --strategy config/strategies/z_gap.yaml \
  --risk config/risk/example_risk.yaml \
  --execution config/execution/example.yaml \
  --runtime config/runtime/observe_btc_5m.yaml \
  --scenario aggressive \
  --mode observe \
  --run-name z_gap_aggressive_test \
  --validate-config

python -m tyrex_pm.application.cli run ... --show-config
python -m tyrex_pm.application.cli run ... --mode observe --run-name z_gap_aggressive_test

# SHADOW (requires enable_oms=true):
python -m tyrex_pm.application.cli run \
  --strategy config/strategies/z_gap.yaml \
  --risk config/risk/example_risk.yaml \
  --execution config/execution/shadow_example.yaml \
  --runtime config/runtime/observe_btc_5m.yaml \
  --scenario aggressive \
  --mode shadow \
  --run-name z_gap_aggressive_shadow

# LIVE (N7 one-shot; defaults fill strategy/risk/execution when omitted):
python -m tyrex_pm.application.cli run --mode live --runtime config/runtime/live_btc_5m.yaml
python -m tyrex_pm.application.cli run --mode live --runtime config/runtime/live_btc_5m.yaml --live
```

If `tyrex-pm` is installed editable (`pip install -e .`), the same arguments work as `tyrex-pm run …`.

`--validate-config` and `--show-config` are mutually exclusive; both use the real resolve path and do not start a host. LIVE_TINY is **not** available on `run` (use N7 commands).

Commented examples under `config/{strategies,risk,execution,runtime,scenarios}/` document every supported field for this slice. Spec: `Docs/implementation/framework_usability_task/`.

## Sources and precedence (legacy JSON observe/shadow)

1. CLI flags (highest for that run)
2. JSON config file (`--config`)
3. Process environment / `.env` (credentials and address roles)
4. Code defaults / policy modules (e.g. `r7_lifecycle_policy`, `ZGapEntryConfig`)

Committed seals:

- `config/r7/acknowledgment_policy.json` — sealed acknowledgment identities (R7 phase-specific)
- `config/n7_tiny_live.json` — N7 Z-Gap Scope A sealed one-shot (mutations default OFF)

Example JSON configs (all exist in-repo):

| File | Typical use |
|------|-------------|
| `config/observe_fixture_r3.json` | Offline observe |
| `config/observe_live_r3.json` | Public live observe |
| `config/observe_shadow_r5.json` | Shadow OMS |
| `config/observe_shadow_r4.json` | Older shadow sample |
| `config/observe_z_gap_fixture_f3.json` | Z-Gap fixture OBSERVE |
| `config/observe_shadow_z_gap_f4.json` | Z-Gap fixture SHADOW |
| `config/n7_tiny_live.json` | N7 Z-Gap tiny live one-shot |

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

## Common JSON fields (observe / shadow)

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

## N7 sealed config (`config/n7_tiny_live.json`)

Loaded by `tyrex_pm.runtime.n7_sealed`. Boolean fields must be JSON booleans (not strings).

| Field | Current default | Meaning |
|-------|-----------------|---------|
| `require_ssr_price_match` | `false` | When `false`, sealed Chainlink K alone is PTB-ready; SSR is not fetched/gated. When `true`, restore strict SSR `openPrice` MATCH. |
| `max_buy_collateral` / `live.hard_collateral_cap` | `"5.00"` | Hard fee-inclusive BUY debit ceiling |
| `max_daily_notional` / `max_daily_loss` | `"5.00"` | Daily bounds (entry exposure accounting) |
| `max_entry_lineages` / `max_positions_per_window` | `1` | One lineage / one position |
| `skip_if_min_exceeds_cap` | `true` | Skip if venue min cannot fit under cap |
| `one_shot` | `true` | Required for N7 |
| `timing.*` | frozen | `FROZEN_FOR_N7` entry/exit/flatten cutoffs |
| `live.enabled` / `live.mutations_enabled` | `false` | Defaults OFF; `--live` arms for one window |
| `live.scope` | `"A"` | Scope A only |
| `z_gap.resolution_capability` | `false` | No hold-to-resolution / redeem |
| `z_gap.target_notional` | `"5"` | Desired share notional (still sized down to fee-inclusive cap) |
| `z_gap.fee_rate` / `fee_exponent` | `"0.07"` / `"1"` | Conservative entry-fee curve for sizing |

### Not yet wired from N7 JSON (code defaults)

Z-Gap **entry signal** thresholds live on `ZGapEntryConfig` / compose defaults and are **not** currently parameterized from `n7_tiny_live.json`:

- `theta_take`, `z_min`, `z_max`, `tau_min_s`, `tau_max_s`, `require_repricing_edge`, …

Changing only the JSON will not relax entry frequency until those fields are plumbed. Live compose currently builds `ZGapConfig` with a widened `basis_max_bps` and otherwise uses class defaults.

### Fee-inclusive cap (always)

```text
worst_price × qty + conservative_entry_fee ≤ 5.00
```

Size downward; SKIP if venue minimum cannot fit; never raise the cap.  
Exit fees may reduce proceeds; they never block inventory-reducing exits.

## Runtime flags (CLI)

| Flag | Command | Meaning |
|------|---------|---------|
| `--config` | observe/shadow/n7-* | JSON path |
| `--mode` | observe | override fixture/live |
| `--btc-window` | observe/shadow | `current` / `next` |
| `--which` | `discover-btc-window` | `current` / `next` |
| `--duration-s` | observe/shadow | live runtime seconds |
| `--output` / `--out-dir` | several | report path |
| `--dry-run` | `r7b-live-once` | no venue mutation (default when not executing) |
| `--execute-live` | `r7b-live-once` | venue mutation (R7 operator only) |
| `--live` | `n7-live` / N7 oneshot tool | venue mutation for one N7 window (operator only) |
| `--fake-rehearsal` | N7 oneshot tool | FakeTransport lifecycle; ignores `--live` |
| `--skip-auth` | `live-preflight` | public connectivity only |

## Paths

```text
var/state/       local persistent operational state (gitignored)
var/reporting/   runtime-disposable evidence (incl. var/reporting/n7/)
config/          committed configs + sealed ack / N7 seals
```

## Secret-handling rules

- Never commit `.env`
- Never log secrets or full addresses
- Prefer suffixes in reports
- Do not paste credentials into documentation
