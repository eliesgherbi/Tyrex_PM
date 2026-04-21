# Operations

**Hub:** [README.md](README.md) · **Architecture:** [Architecture.md](Architecture.md) · **Live truth:** [LIVE_ARCHITECTURE.md](LIVE_ARCHITECTURE.md) · **Reporting:** [reporting_fact_model.md](reporting_fact_model.md)

How to run, configure, and observe Tyrex_PM in shadow and live modes.

---

## 1. CLI

Three subcommands, all backed by `tyrex_pm.runtime.app`:

```bash
tyrex-pm run [...]            # full guru-follow loop (shadow or live)
tyrex-pm live-attest [...]    # one-shot live submit + cancel attestation
tyrex-pm reset-state [...]    # clear local on-disk state (V2 cutover hygiene)
# Equivalent: python -m tyrex_pm.runtime.app <cmd> [...]
```

### 1.1 `tyrex-pm run`

| Flag | Default | Purpose |
|------|---------|---------|
| `--strategy` | `config/strategies/guru_follow.yaml` | strategy YAML (relative to repo root) |
| `--scenario` | none | overlay YAML (bare name or path); deep-merged into risk/runtime/strategy |
| `--repo-root` | auto (3 parents up from `app.py`) | override repo root for non-standard layouts |
| `--state-dir` | `var/state` | location of `guru_strategy_store.json` (watermark + dedup) |
| `--once` | off | single poll iteration then exit |
| `--fixture <path>` | none | replay a Data API JSON file (shadow only) |
| `--max-iterations N` | none | stop after N poll loops |
| `--run-name <label>` | none | use `<label>` as the run directory name (sanitized); `run_id` in facts is still a fresh UUID |

### 1.2 `tyrex-pm live-attest`

Minimal post + cancel using the same `SingleWriterOMS` / `LiveOMS` / heartbeat / venue refresh / user-WS stack — **does not** use guru polling.

| Flag | Default | Purpose |
|------|---------|---------|
| `--token-id <numeric>` | required | numeric Polymarket CLOB outcome token id |
| `--size`, `--price` | required | order parameters |
| `--side` | `BUY` | `BUY` or `SELL` |
| `--scenario` | `live_attest` | uses `config/scenarios/live_attest.yaml` (capital gate off, venue_min_size off) |
| `--readiness-timeout-s` | 120 | overrides `TYREX_LIVE_ATTEST_READINESS_S` |

`live_attest` writes `live_attest` facts and a `run_summary.json` with `outcome` + `venue_order_id` on success. Exit code `0` = post + cancel completed; non-zero = bootstrap, readiness, risk, submit, parse, or cancel failure.

```bash
tyrex-pm live-attest \
  --scenario live_attest \
  --token-id "9088321..." \
  --size "1" --price "0.01" --side BUY
```

**SELL attest** requires venue inventory or risk denies with `naked_sell`; prefer **BUY** for first attestation.

### 1.3 `tyrex-pm reset-state`

Clears local on-disk state files that a future `tyrex-pm run` would consume. Reporting artifacts under `var/reporting/runs/` are **never** touched (immutable history). Idempotent: a clean tree is a no-op.

| Flag | Default | Purpose |
|------|---------|---------|
| `--state-dir` | `var/state` | directory whose documented state files will be deleted |
| `--repo-root` | auto | resolve `--state-dir` relative to this root |

```bash
tyrex-pm reset-state                          # default: var/state
tyrex-pm reset-state --state-dir var/state    # explicit
```

Currently removes: `guru_strategy_store.json` (guru watermark + dedup ledger). Run this before the first live process on a fresh V2 environment so no V1-era guru cursor leaks into the V2 startup. Bootstrap is also enforced in code: until the first successful V2 venue truth rebuild, `check_aggressive_readiness` denies with `bootstrap_not_complete`.

---

## 2. Configuration

Layered YAML merge (later overrides earlier):

```
config/risk/default.yaml
config/runtime/default.yaml
config/strategies/<file>.yaml      # via --strategy
config/scenarios/<file>.yaml       # via --scenario (deep-merged)
```

Bundled scenarios:

| Scenario | Mode | Notable overrides |
|----------|------|-------------------|
| `shadow_guru` | shadow | `runtime.shadow_bootstrap` USDC seed; faster `data_api_poll_interval_s`. |
| `live_guru` | live | wider `deployment.token_cap_usd: 100`. |
| `live_attest` | live | `capital.enabled: false`, `venue_min_size.enabled: false`, tight notional, `readiness.require_user_ws_live: false`. |

Authoritative reference for every key: [CONFIG_MODEL.md](CONFIG_MODEL.md).

---

## 3. Environment

`tyrex_pm.runtime.app._maybe_load_dotenv` loads `./.env` (cwd) first, then `<repo_root>/.env`, when `python-dotenv` is installed.

### 3.1 Live signing

| Variable | Notes |
|----------|-------|
| `TYREX_PRIVATE_KEY` | Required for live / `live-attest`. **`POLYMARKET_PK`** is accepted as fallback. |
| `TYREX_FUNDER` | Optional proxy/funder address. **`POLYMARKET_FUNDER`** fallback. |
| `TYREX_CLOB_HOST` | Default `https://clob-v2.polymarket.com` (V2 staging; flipped to `https://clob.polymarket.com` on V2 cutover day). |
| `TYREX_CHAIN_ID` | Default `137`. |
| `TYREX_SIGNATURE_TYPE` | Default `0` (EOA). `1=POLY_PROXY`, `2=POLY_GNOSIS_SAFE`, `3=POLY_1271`. Use a non-EOA value **with** `TYREX_FUNDER` = proxy/Safe address if orders fail with `invalid signature`. **`POLYMARKET_SIGNATURE_TYPE`** fallback. |
| `TYREX_BUILDER_CODE` / `TYREX_BUILDER_ADDRESS` | Optional V2 builder attribution (32-byte hex code + 20-byte address). Both required when set; malformed values fail fast. |

### 3.2 Heartbeat / venue refresh

| Variable | Default | Purpose |
|----------|---------|---------|
| `TYREX_HEARTBEAT_INTERVAL_S` | `8` | Clamped to ≥ 5 s; smaller intervals cause alternating 200/400 from the venue. |
| `TYREX_HEARTBEAT_ID` | random 32-char hex | Hyphenated UUIDs are normalized to hex. **`POLYMARKET_HEARTBEAT_ID`** fallback. |
| `TYREX_VENUE_REFRESH_S` | `reconcile_interval_s` | REST wallet refresh cadence. |

### 3.3 User WebSocket / staleness

| Variable | Default | Purpose |
|----------|---------|---------|
| `TYREX_USER_WS_DISABLE` | unset | `1` skips user WS; set scenario `readiness.require_user_ws_live: false`. |
| `TYREX_USER_WS_STALE_S` | `45` | Mark `venue_truth_stale` after this many seconds without WS messages. |
| `TYREX_USER_WS_GRACE_S` | `20` | Grace before the first WS message arrives. |

### 3.4 Provisional repair (rare overrides)

Settings normally come from `config/runtime/default.yaml` (`supervisors.*`). Env aliases:

| Variable | Maps to |
|----------|---------|
| `TYREX_SUBMIT_GRACE_S` / `TYREX_VENUE_CONFIRM_GRACE_S` (alias) | `supervisors.submit_grace_s` |
| `TYREX_PROVISIONAL_UNKNOWN_TERMINAL_TIMEOUT_S` / `TYREX_VENUE_CONFIRM_PROVISIONAL_TIMEOUT_S` (alias) | `supervisors.provisional_unknown_terminal_timeout_s` |
| `TYREX_ADOPTION_GRACE_S` | `supervisors.adoption_grace_s` |

### 3.5 Other

| Variable | Default | Purpose |
|----------|---------|---------|
| `TYREX_DATA_API_BASE` | `https://data-api.polymarket.com` | override for proxies / mocks |
| `TYREX_LIVE_ATTEST_READINESS_S` | from `--readiness-timeout-s` | maximum wait for live-attest aggressive readiness |
| `TYREX_LIVE_SMOKE` | unset | `1` opts the optional pytest live heartbeat smoke test in (not full attest) |

Full template: [`.env.example`](../.env.example).

---

## 4. Run directory layout

Each run writes `var/reporting/runs/<run_id_or_name>/`:

```
manifest.json          # run_id, schema_version, git_sha, execution_mode, run_kind, run_name
facts.jsonl            # one fact per line; see reporting_fact_model.md
run_summary.json       # iterations + last guru_poll snapshot (run_kind="tyrex_run")
                       # or attest outcome + venue_order_id (run_kind="live_attest")
```

`--run-name <label>` controls the directory name only; the `run_id` field on every fact is still a fresh UUID. The label is sanitized (path separators, control characters, whitespace stripped, max 120 chars).

---

## 5. Reading `facts.jsonl`

Common operator queries:

```bash
# Why was a signal denied?
grep '"fact_type":"risk_decision"' var/reporting/runs/<id>/facts.jsonl | tail
grep '"approved":false' var/reporting/runs/<id>/facts.jsonl

# What did the venue actually accept / reject?
grep '"fact_type":"oms_submit"' var/reporting/runs/<id>/facts.jsonl
grep '"fact_type":"oms_reject"' var/reporting/runs/<id>/facts.jsonl

# Did reconciliation flag drift?
grep '"fact_type":"reconcile"' var/reporting/runs/<id>/facts.jsonl | grep -i blocking

# Wallet refresh snapshots (operator-meaningful state changes only)
grep '"fact_type":"wallet_sync"' var/reporting/runs/<id>/facts.jsonl
```

Join keys (consistent across fact types): `run_id`, `correlation_id` (= guru `dedup_key` on guru-driven runs, `live-attest` on attest), `client_order_id`, `venue_order_id`, `submit_fingerprint`.

Canonical OMS payload key inside `oms_submit` / `oms_cancel`: **`oms_result`** (string). `oms_reject` carries `error_msg` + `status_code`. Older runs may have `shadow_result`; both `summarize_run` and `reporting.oms_payload.get_oms_result_text` handle either.

Full fact catalog and dedup rules: [reporting_fact_model.md](reporting_fact_model.md).

### 5.1 Summarize a run (Python API)

There is no `tyrex-pm summarize` subcommand. From repo root:

```python
from pathlib import Path
from tyrex_pm.reporting.summarize import summarize_run
print(summarize_run(Path("var/reporting/runs/<run_id>")))
```

Returns: `operator_view` (hollow-run hints), `join_audit`, `run_summary_json`, fact counts.

---

## 6. Common operational outcomes

### 6.1 "Hollow run" — only `health` + `guru_poll` facts

Either nothing new arrived (watermark + dedup absorbed all rows) or the guru wallet is unset / placeholder. Check `last_guru_poll` in `run_summary.json`:

| Pattern | Likely cause |
|---------|--------------|
| `raw_rows=0` | empty Data API page for that wallet |
| `raw_rows>0, new_signals=0` | dedup / watermark filtered everything |
| `guru_wallet_configured=false` | `strategy.guru.wallet` is the placeholder |

`summarize_run().operator_view.notes` annotates these automatically.

### 6.2 Risk denials (`fact_type=risk_decision`, `approved=false`)

`reason_codes` holds the stable code (see [`core/reason_codes.py`](../src/tyrex_pm/core/reason_codes.py)). The full evidence (notional policy, deployment USD, capital math, in-flight reservation totals, venue-min-size policy) is in `extensions` / top-level keys of the payload. Common live denials:

| Reason | Where to look |
|--------|---------------|
| `not_ready` | check `wallet_sync` cadence, heartbeat, user-WS staleness facts |
| `portfolio_deployment_cap` / `token_deployment_cap` | check `per_token_deployed_usd`, `portfolio_deployed_usd`, `in_flight_reserved_usd_total` in the same fact |
| `insufficient_capital` / `insufficient_allowance` | check `wallet_usdc_balance`, `wallet_usdc_allowance`, `effective_free_balance_usd` |
| `naked_sell` | `inventory.sell_requires_venue_position: true` and we don't see the position yet |
| `below_venue_min_size` | strategy sized below 5 shares; switch policy to `bump` if appropriate |
| `kill_switch` | `risk.kill_switch.enabled: true` |
| `duplicate_submit_blocked` | provisional row with same fingerprint still in repair |

### 6.3 Venue rejects (`fact_type=oms_reject`)

Carries `status_code` + `error_msg`. The pipeline does **not** crash; it releases the in-flight reservation, increments `venue_restart_suspected` on 425, and continues.

### 6.4 Reconcile drift

`reconcile_blocks_live=true` means the next `risk_decision` will deny with `reconcile_drift` until resolved. Look at `blocking_drift_flags`, `provisional_repair_decisions`, `venue_adoption_decisions`, and `tombstoned_rest_vids` in the same fact for the cause. Full state machines: [LIVE_ARCHITECTURE.md](LIVE_ARCHITECTURE.md).

---

## 7. First-time live checklist

1. `pip install -e .[live]`.
2. Fill `.env` from `.env.example` (`TYREX_PRIVATE_KEY` + `TYREX_FUNDER` if proxy wallet, `TYREX_SIGNATURE_TYPE=1` for proxy).
3. Verify env: `tyrex-pm live-attest --token-id <real_numeric_token> --size 1 --price 0.01 --side BUY`.
4. Inspect `var/reporting/runs/<latest>/facts.jsonl` for `live_attest` `outcome` + `venue_order_id`.
5. Edit `config/strategies/guru_follow.yaml` — set `guru.wallet` to a real address; tune `filters` and `sizing`.
6. Start small: `tyrex-pm run --scenario live_guru --run-name first_live --max-iterations 5`.
7. Tail `facts.jsonl`; confirm `risk_decision`, `oms_submit`, `wallet_sync`, `reconcile` cadence.
8. Scale up by lifting `risk.deployment.*` caps in a custom scenario.

---

## 8. Planned / not yet shipped

- `tyrex-pm summarize` CLI subcommand (use the Python API for now).
- Kill-switch drill runbook + incident templates.
- Built-in protection / virtual TP-SL overlays (intentionally absent today; strategies own exits).
