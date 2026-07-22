# N7A implementation and preflight report

**Final verdict:** `PASS_N7A` (implementation + deterministic + auth RO).  
**N7B:** not authorized — stop for operator phrase.

---

## 1. Baseline

| Item | Value |
|------|-------|
| Branch | `rest_project` |
| Start HEAD | `9ed2729` (*Add generic live OMS composition and reconciliation for strategies*) |
| Worktree at start | clean |
| Python | 3.12.3 conda-forge (`C:\Users\windo\miniconda3\python.exe`) |
| Prior suite | 618 passed |
| Ending suite | 649 passed |

HEAD matched expected `9ed2729`.

---

## 2. Ownership mapping

| N7 concern | Existing owner | Required evolution | Reuse/adapt/new | Reason |
|------------|----------------|--------------------|-----------------|--------|
| Live Scope A host | `runtime/n6_live_host.py` | Wrap for one-shot/auth | adapt | Generic Risk→OMS→fills→lifecycle |
| Live config | `runtime/live_config.py` | Caps via sealed N7 | adapt | Defaults OFF; Scope B refused |
| Timing ladder | `runtime/scope_a_ladder.py` + `n7_timing.py` | Freeze numerics | adapt + new | Structure reused; values frozen |
| Fake mutation auth | `runtime/n6_authorization.py` | Fake arm only | reuse | N7A rehearsal |
| Operator envelope | — | Single-use ceremony | **new** `n7_authorization.py` | Not reusable from R7 |
| One-shot host | — | Bind-once + ladder | **new** `n7_oneshot_host.py` | Window/lineage/daily limits |
| Abort codes | — | Stable enum | **new** `n7_abort.py` | Operator report |
| Sealed config | — | Production freeze | **new** `n7_sealed.py` + `config/n7_tiny_live.json` | Fingerprintable |
| Preflight | `live_preflight` + N6 classify | Dual recon + auth request | adapt | Mutations OFF |
| Lineage | `execution/lineage.py` | Unchanged | reuse | Venue idempotency insufficient |
| Account classify | `n6_account_classify.py` | Historical externals | reuse | Four positions visible |
| CLI | `application/cli.py` | n7-* ceremony | adapt | No silent approve |
| Strategy | Z-Gap intents only | Unchanged | reuse | No venue types in strategy |
| R7 | `runtime/r7*` | Concepts only | do not import | Historical |

---

## 3. Frozen production parameters

**Status:** `FROZEN_FOR_N7` (no OPEN production values in sealed config).

| Parameter | Value | Justification |
|-----------|-------|-----------------|
| `max_buy_collateral` | 5.00 USDC | Hard maximum (N7 design); never auto-raise |
| `max_daily_notional` | 5.00 | One-shot entry exposure only |
| `max_daily_loss` | 5.00 | Worst-case full entry loss |
| Daily notional accounting | Entry only | Inventory-reducing exits do not add notional |
| `skip_if_min_exceeds_cap` | true | SKIP when venue min > cap |
| `order_style` | marketable_limit | Decision #9 |
| `ack_timeout_ms` | 15000 | N1 boundary lag ≤5.5s + margin |
| `last_allowed_entry_before_end_s` | 180 | Prep ≥30–60s; ~120s usable on 300s window |
| `discretionary_exit_cutoff_before_end_s` | 120 | After last entry |
| `mandatory_flatten_start_before_end_s` | 90 | Force exit with skew slack |
| `residual_operator_deadline_before_end_s` | 45 | Operator escalate |
| `event_end_safety_buffer_s` | 30 | N5 used 20s + margin |
| `acknowledgment_timeout_s` | 15 | Matches ack_timeout |
| `cancel_recon_budget_s` | 10 | Cancel+recon RTT |
| `exit_retry_max_attempts` | 3 | Decision #25 |
| `exit_retry_time_budget_ms` | 60000 | Within flatten window |
| `resolution_capability` | false | Scope A |
| `max_entry_lineages` | 1 | One-shot |
| Market family | `btc_updown_5m` | Envelope-bound |

Config fingerprint (sealed file):  
`db4599d2135a760e7c801c119f22775c6bab8b99966686eadc02a25189848f83`

---

## 4. Files created / changed

**Created:** `n7_abort.py`, `n7_timing.py`, `n7_git.py`, `n7_sealed.py`, `n7_authorization.py`, `n7_oneshot_host.py`, `n7_preflight.py`, `config/n7_tiny_live.json`, `tools/n7_live/*`, `tests/helpers_n7.py`, `tests/test_n7_*.py`, this report.

**Modified:** `scope_a_ladder.py` (status FROZEN), `application/cli.py` (`n7-status`, `n7-preflight`, `n7-auth-request`, `n7-oneshot`), `n7_tiny_operator_live.md` status, initiative README.

---

## 5. Tests

```text
python -m pytest tests/test_n7_config_auth.py tests/test_n7_oneshot.py tests/test_n7_cli.py -q
# focused N7A

python -m pytest tests -q --tb=no
649 passed in 19.55s
```

---

## 6. Fake one-shot rehearsal

```text
python tools/n7_live/run_n7_fixture_acceptance.py --out-dir var/reporting/n7/fixture_n7a
```

| Check | Result |
|-------|--------|
| Entry/exit ACK | yes |
| Fill prices drive portfolio | yes (confirmed fills) |
| Final flat | true |
| Envelope consumed once | true |
| `allows_real_venue_mutation` | false |
| `real_venue_mutations` | 0 |
| Mutations force OFF after terminate | true |

---

## 7. Real authenticated read-only preflight

```text
python tools/n7_live/run_n7_readonly_preflight.py --out-dir var/reporting/n7/readonly_n7a --allow-dirty
```

(`--allow-dirty` only for N7A implementation worktree; N7B requires clean committed HEAD.)

| Check | Result |
|-------|--------|
| `go_no_go` | GO |
| Dual preflight | first_ok / restart_ok |
| User stream auth + reconnect | true |
| Balances / allowance fields | retrieved |
| Open orders | 0 |
| Positions | 4 (acknowledged historical) |
| Selected-market flat | true |
| Classifications stable | true |
| Mutations | 0 |
| Auth request generated | `authorization_request.json` |

### Window / PTB / feeds probe

```text
python tools/n3_ptb/run_n3_ptb_live.py --min-seals 1 --wait-for-boundary \
  --prep-lead-s 45 --max-duration-s 120 \
  --out-dir var/reporting/n7/readonly_n7a/window_ptb
```

| Check | Result |
|-------|--------|
| Discovery | active + prepared BTC 5m slugs |
| Chainlink / Binance / CLOB book events | 72 / 5697 / 27883 |
| Exact seal | `EXACT_AT_START` on prepared window |
| Attestation | `MATCH` (SSR openPrice) |
| Immutable check | ok |
| `auth_touched` / `orders_live` / `venue_mutation` | false / 0 / false |

One mid-window miss recorded for the window already in progress at probe start (expected).

---

## 8. Confirmations

- No real order submitted/cancelled
- No redeem / allowance / transfer
- Historical positions untouched
- `.env` unchanged; secrets not printed
- Scope B / hold / continuous live not enabled
- N7B not executed
- Nothing pushed

---

## 9. N7B gate

N7A does **not** authorize real venue mutation.  
After this commit is clean on HEAD, regenerate `n7-auth-request` and present the operator authorization package. Proceed to N7B only with the exact phrase.
