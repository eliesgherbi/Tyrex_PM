# N6 acceptance report — generic live OMS + reconciliation

**Final verdict:** `PASS`

Gates 1, 2, and 3 **PASS**. Authenticated read-only reconciliation succeeded
with mutations disabled (`gate3_status=LIVE_READONLY_OK`). N6 composition is
accepted for **N7 planning**. N7 itself remains unimplemented; no real-money
mutations are enabled.

---

## 1. Baseline

| Item | Value |
|------|-------|
| Branch | `rest_project` |
| Expected HEAD | `328da5c` |
| Actual start HEAD | `e5e039e` (one doc commit after `328da5c`; clean) |
| Python | 3.12.3 conda-forge |
| Prior tests | 575 passed |

Difference from expected HEAD: commit `e5e039e` only records ending HEAD in the
N3B/N4B/N5B live-input validation report. No functional drift.

---

## 2. What existed / what was missing

**Existed (generic venue layer):** LiveOMS, FakeTransport, ReconciliationService,
auth/user-stream RO, OrderStore, FillLedger, Portfolio, TradeLifecycle, RiskEngine,
planners, live-preflight, ShadowOMS/N5.

**Missing:** generic non-r7 LiveHost, `live.*` config, Scope A timing ladder,
framework lineage registry, MutationAuthorization, N6 operator commands,
idempotency audit artifact, account classification for acknowledged externals.

**R7:** concepts only — Z-Gap/N6 must not import `runtime/r7*`.

---

## 3. R7 concept → generic ownership

| R7 mechanism/concept | Current owner | Generic destination | Reuse/adapt/rewrite | Reason |
|----------------------|---------------|---------------------|---------------------|--------|
| LiveOMS submit/ack/uncertain | `execution/polymarket/live_oms.py` | same | reuse | Already strategy-agnostic |
| FakeTransport scenarios | `fake_transport.py` | same | reuse | Deterministic venue I/O |
| ReconciliationService | `reconciliation.py` | same + N6 host wiring | reuse | Open-order/fill/position diffs |
| Auth / L2 / signer vs funder | `auth.py`, `address_roles.py` | same | reuse | Read-only identity |
| User stream observe | `user_stream_readonly.py` | same | reuse | Auth channel without mutations |
| Mutation arm token | R7 `MutationArmToken` | `runtime/n6_authorization.py` | rewrite | N6 forbids real venue; fake-only |
| One-shot mutation lifecycle | `mutation_lifecycle.py` / r7b | `runtime/n6_live_host.py` | rewrite | Generic Scope A host |
| Session / ack ceremonies | `runtime/r7*` | leave historical | do not import | Phase-specific ReferenceMomentum |
| Live-once CLI | `r7b-live-once` | **not** mirrored for Z-Gap | refuse | N6 has no execute-live for Z-Gap |
| Position acknowledgment | r7 ack gate | `n6_account_classify.py` | adapt | Visible acknowledged externals |
| Timing / flatten policy | r7 lifecycle constants | `scope_a_ladder.py` | rewrite | Structure only; numerics OPEN |
| Budget / hard cap | `live_budget.py` / R7 | LiveConfig hard_collateral_cap | adapt | SKIP if min valid > cap |
| Preflight composition | `live_preflight.py` | CLI `n6-preflight` / tools | reuse | Mutations impossible |
| Persistence fingerprint | `StateSnapshotStore` | N6LiveHost recover | reuse | Refuse mismatched fingerprint |

---

## 4. Idempotency audit

**Classification:** `VENUE_IDEMPOTENCY_NOT_AVAILABLE_OR_INSUFFICIENT`

Evidence:

- Official Polymarket `POST /order` schema has no caller-controlled idempotency key
  ([docs](https://docs.polymarket.com/api-reference/trade/post-a-new-order)).
- `py-clob-client` `OrderArgs` exposes `nonce` (on-chain cancel) and order `salt`
  for EIP-712 hashing — not a client-order-id retry key.
- Duplicate identical signed orders may return `INVALID_ORDER_DUPLICATED`; lost-ack
  recovery cannot rely on venue idempotency.
- NautilusTrader integration notes: venue does not expose an idempotency key for
  batch retry.

**Authoritative for N6:** framework lineage

`intent_id → plan_id → request_fingerprint → submission_attempt_id → venue_order_id?`

Ambiguous ack → `AMBIGUOUS` → recon; never fresh duplicate entry.

Artifact: `n6_idempotency_audit.md`.

---

## 5. Gate table

| Gate | Changes | Commands | Expected | Actual | Verdict | Evidence |
|------|---------|----------|----------|--------|---------|----------|
| **1 Architecture** | live_config, lineage, ladder, authorization, account classify, arch tests | `pytest tests/test_n6_live_config.py tests/test_n6_account_classify.py -q` | Fail-closed defaults; no z_gap→r7/execution.polymarket; scope B refused | Pass | **PASS** | tests |
| **2 Fake Scope A** | N6LiveHost + scenarios + fixture CLI | `pytest tests/test_n6_scope_a_lifecycle.py -q`; `python tools/n6_live/run_n6_fixture_acceptance.py` | Full lifecycle on FakeTransport; mutations 0 real | focused tests green; fixture FLAT; `real_venue_mutations=0` | **PASS** | `var/reporting/n6/fixture_gate2/` |
| **3 Auth RO** | readonly recon tool + CLI | `python tools/n6_live/run_n6_readonly_recon.py --out-dir var/reporting/n6/readonly_manual` | Auth + dual recon; mutations off | `gate3_status=LIVE_READONLY_OK`; dual `preflight_ok=true`; user stream auth+reconnect; balances/orders/positions read | **PASS** | `var/reporting/n6/readonly_manual/` |

---

## 6. Files created / changed

**Created**

- `src/tyrex_pm/runtime/live_config.py`
- `src/tyrex_pm/runtime/scope_a_ladder.py`
- `src/tyrex_pm/runtime/n6_authorization.py`
- `src/tyrex_pm/runtime/n6_live_host.py`
- `src/tyrex_pm/runtime/n6_account_classify.py`
- `src/tyrex_pm/execution/lineage.py`
- `tools/n6_live/*`
- `tests/helpers_n6.py`, `test_n6_live_config.py`, `test_n6_scope_a_lifecycle.py`, `test_n6_account_classify.py`
- This report + `n6_idempotency_audit.md`

**Modified**

- `src/tyrex_pm/application/cli.py` — `n6-status`, `n6-preflight`, `n6-recon`, `n6-kill-inspect`
- N6 design doc status

---

## 7. Scope A control flow (generic)

```text
preflight recon (empty/known)
→ readiness (kill/UNKNOWN/ladder/ambiguity)
→ EnterIntent → RiskEngine → ExecutionPlanner (hard cap; SKIP if min>cap)
→ lineage register → LiveOMS.submit (FakeTransport only when authorized)
→ ack | reject | AMBIGUOUS
→ confirmed fill events (fill price ≠ submitted limit) → Portfolio/Lifecycle ACTIVE
→ ExitIntent/Flatten (inventory-bounded) → exit fill → FLAT
→ post-trade recon (fee_label estimated|confirmed; pnl confirmed fills only)
```

---

## 8. Gate 3 authenticated read-only (PASS)

Command:

```bash
python tools/n6_live/run_n6_readonly_recon.py --out-dir var/reporting/n6/readonly_manual
```

**Summary (`readonly_summary.json`):**

| Check | Result |
|-------|--------|
| `gate3_status` | `LIVE_READONLY_OK` |
| first / restart `ok` | true / true |
| `classification_stable` | true |
| Classifications | `KNOWN_FLAT_SELECTED_MARKET`, `KNOWN_ACKNOWLEDGED_EXTERNAL_POSITIONS` |
| Mutations / submit / cancel / redeem / allowance | all false / 0 |
| User stream | connected, authenticated, pong, disconnect+reconnect observed |
| `ready_for_n7_mutations` | false (N6 does not arm mutations) |

**Preflight evidence (`preflight_1.json` / `preflight_2_restart.json`):**

| Check | Result |
|-------|--------|
| Public CLOB `/time`, `/book` | ok (prior blocker: local DNS→`*.anj.fr` cert; fixed on operator host) |
| Credentials present | true |
| Identity | signer derives; signer ≠ funder; funder present; mapping match |
| Transport | `official_py_clob_client_v2_readonly` |
| Auth ops | `get_open_orders`, `get_trades`, `get_balance`, `get_positions` all ok |
| Balance evidence | retrieved; allowance field present; collateral nonzero (amounts not logged) |
| Open orders | 0 |
| Positions | 4 rows / 4 nonzero (acknowledged historical; not strategy inventory) |
| Trades read | 1035 (count only) |
| Recon | `POSITION_MISMATCH: 4`, `observation_only_local_empty=true`, `blocks_entry_if_trading=true` |
| Readiness reasons | `MUTATIONS_DISABLED` only |
| Heartbeat POST | not called |
| Secrets | redacted; `.env` unchanged |

Historical acknowledged externals (`historical_lol`, three `historical_btc_5m_*`)
remain visible, untouched, and not treated as strategy inventory.

---

## 9. Tests

```text
python -m pytest tests/test_n6_live_config.py tests/test_n6_scope_a_lifecycle.py tests/test_n6_account_classify.py -q
43 passed

python -m pytest tests -q --tb=no
618 passed in 22.47s
```

---

## 10. OPEN for N7

- Production timing-ladder numerics
- Ack timeout production value
- Maximum notional / hard cap operator freeze
- Order style freeze (marketable limit / FAK)
- Market binding for first tiny live
- Operator authorization envelope
- Residual handling policy
- Explicit N7 enablement (still no Z-Gap `--execute-live` in N6)

---

## 11. Confirmations

- No real order submitted or cancelled
- No position altered / redeemed / allowance changed
- Historical acknowledged positions untouched (classified visible)
- `.env` unchanged; secrets not printed
- Scope B not implemented
- N7 not implemented or executed
- Nothing pushed

---

## 12. What remains before one tiny Z-Gap real-money lifecycle

1. N7 authorization envelope + Scope A timing freeze (operator-controlled).
2. Still no Z-Gap `--execute-live` until N7 deliberately enables it.
3. First live run remains an ops event after N7 acceptance — not N6.
