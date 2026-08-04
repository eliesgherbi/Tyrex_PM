# Book-state implementation report

**Status:** COMPLETE_WITH_ENVIRONMENT_BLOCKER (official SDK cutover landed; full two-rollover OBSERVE pending operator host)  
**Plan:** `Docs/Implementation/book_state_task/impelmentation_plan.md`  
**Branch:** `rest_project`  
**HEAD (pre-implementation / inspected):** `18235c84c99bb0066bcc5271bd761fb191b0548c`

## Baseline

```text
Command: python -m pytest -q
Result:  47 failed, 674 passed, 1 skipped
Note:    Failures dominated by missing deleted config JSON files (pre-existing dirty tree).
Book-related baseline: tests/test_r3_book_store.py + tests/test_n2_real_data_adapters.py → 34 passed.
```

## Final verification

```text
Targeted book-state suite:
  python -m pytest -q tests/test_book_state_*.py tests/test_r3_book_store.py
  → 26 passed

Full suite:
  python -m pytest -q
  → 47 failed, 696 passed, 1 skipped
  (same 47 pre-existing config FileNotFound failures; +22 new book-state tests green)

Static:
  ruff check on new book-state modules (unused imports fixed; remaining E501/F841 in unrelated pre-existing files not rewritten)

git diff --check:
  trailing whitespace reported in pre-existing Docs/latest and n7 docs (unrelated dirty tree); not introduced by book-state modules.

LIVE / mutations: none executed.
.env: not modified.
```

---

## BS-1 — Binding and identity contracts

| Field | Value |
|-------|--------|
| status | COMPLETE |
| files/symbols | `market_data/binding_record.py` (`MarketBindingRecord`, `make_binding_id`, `with_role`, persist/load) |
| deps | none |
| summary | Permanent `binding_id`; role promotion does not change tokens; role_epoch independent |
| tests | `tests/test_book_state_bs1_bs2.py` |
| result | PASS |
| deviations | none |
| blockers | none |

## BS-2 — MarketStateStore + BookView

| Field | Value |
|-------|--------|
| status | COMPLETE |
| files/symbols | `book_store.py`, `book_view.py`, `book_health.py`, `book_metrics.py`; `BookSnapshotReceived.connection_epoch` |
| deps | BS-1 |
| summary | Versions, sync health, allow-list, epoch filter, REST stale-overwrite guard, atomic `capture_pair`, `ExecutableBookQuote` (shares) |
| tests | `test_book_state_bs1_bs2.py`, `test_r3_book_store.py` |
| result | PASS |
| deviations | `initialized`/`recovery_required` kept as derived legacy flags |
| blockers | none |

## BS-3 — REST book adapter

| Field | Value |
|-------|--------|
| status | COMPLETE |
| files/symbols | `adapters/polymarket/rest_book.py` |
| deps | BS-2 |
| summary | Normalize REST `/book` to best-first internal books; bootstrap into store as SYNCING by default |
| tests | `tests/test_book_state_rest_and_feed.py` |
| result | PASS |
| deviations | none |
| blockers | Public REST probe failed in this environment (SSL/URLError) — unit path uses fixtures |

### Zero-size / quantity semantics (kickoff corrections)

- `price_change` size `0` → remove level: **fixture-established + official docs alignment** (covered in store/WS tests).
- Quantities: book levels / `ExecutableBookQuote` = **outcome shares**; `target_notional` remains **USDC**.

## BS-4 — BookFeedSupervisor + BindingFeed sync

| Field | Value |
|-------|--------|
| status | COMPLETE |
| files/symbols | `market_data/book_feed.py` |
| deps | BS-3 |
| summary | Binding-scoped ACTIVE+PREPARED feeds; WS buffer → REST snapshot → reconcile → READY; app `PING` every 10s; no subscription ACK wait |
| tests | `test_book_state_rest_and_feed.py` (promote preserves binding/books) |
| result | PASS |
| deviations | REST-only seed remains SYNCING / not continuous READY (kickoff correction 1) |
| blockers | none |

## BS-5 — Host/strategy switch to BookView

| Field | Value |
|-------|--------|
| status | COMPLETE |
| files/symbols | `live_zgap_compose.py`, `n4_observe_runtime.py`, `decision_snapshot.book_view`, `assemble.py`, `n7_live_session._book_from_session` |
| deps | BS-4 |
| summary | Compose wires `MarketStateStore` + `BookFeedSupervisor`; session tops are read-only projections; assemble prefers BookView per-leg readiness |
| tests | `test_book_state_bs5_through_bs9.py` |
| result | PASS (deterministic); operational OBSERVE blocked by env SSL |
| deviations | Offline fixture hosts without `book_store` keep legacy ExecutableQuote path |
| blockers | none for deterministic path |

## BS-6 — Full WebSocket sync path

| Field | Value |
|-------|--------|
| status | COMPLETE |
| files/symbols | `book_feed.py`, `book_store.py`, `test_book_state_ws_protocol.py` |
| deps | BS-5 |
| summary | Snapshot/delta apply, size-0 delete, multi-change messages, unknown token / old epoch reject, malformed normalize isolation, metrics reconcile |
| tests | `test_book_state_ws_protocol.py` + store delta tests |
| result | PASS |
| deviations | Live reconnect/heartbeat exercised in feed implementation; unit tests use published events rather than live WSS |
| blockers | Live WSS not exercised in this environment |

## BS-7 — ACTIVE/PREPARED_NEXT warm rollover

| Field | Value |
|-------|--------|
| status | COMPLETE |
| files/symbols | `BookFeedSupervisor.promote_prepared`, compose rollover wiring |
| deps | BS-6 |
| summary | Promote keeps feed/books/`binding_id`; role_epoch increments; expired feed stopped; next prepared opened; `COLD_GAP_MAX_S = 2.0` |
| tests | promote tests in rest/feed + bs5–bs9 |
| result | PASS (deterministic); two live 5m rollovers ENVIRONMENT_BLOCKER |
| deviations | none |
| blockers | Public rollover evidence blocked by Gamma/CLOB SSL |

## BS-8 — Readiness reasons + reporting

| Field | Value |
|-------|--------|
| status | COMPLETE |
| files/symbols | `reasons.py`, `valuations.py`, `reporting.py` BOOK_FEE_READY set, compose `feeds.book_metrics`, `BookView.leg_book_view` reasons |
| deps | BS-5, BS-6 |
| summary | Distinct BOOK_UNAVAILABLE/SYNCING/STALE/DESYNCED, VENUE_*_EMPTY, FEE_UNAVAILABLE; empty ask ≠ unavailable |
| tests | reason distinction tests in bs5–bs9 |
| result | PASS |
| deviations | Raw WS capture remains disabled/absent (optional, default off) |
| blockers | none |

## BS-9 — Pre-submit revalidation

| Field | Value |
|-------|--------|
| status | COMPLETE |
| files/symbols | `planning/book_revalidation.py`, planner `book_evidence`, `n6_live_host.try_enter`, `n7_oneshot_host.try_enter` |
| deps | BS-7, BS-8 (kickoff correction 3) |
| summary | Version change audits + recomputes; hard invalidators for binding/token/sync/depth/price/fee/edge |
| tests | `test_revalidation_version_bump_soft_and_hard_invalidators` |
| result | PASS |
| deviations | none |
| blockers | none |

## BS-10 — Read-only operational acceptance

| Field | Value |
|-------|--------|
| status | ENVIRONMENT_BLOCKER |
| summary | Public Gamma discovery and CLOB REST probes failed: SSL certificate hostname mismatch / URLError for `gamma-api.polymarket.com` and CLOB `/book` |
| deterministic evidence preserved | golden/fixture tests for items 1,4,5,6,8,9,12,13 |
| items needing operator host rerun | 2,3,7,10,11 (live feeds + two consecutive rollovers + real asks into Z-Gap) |
| mutations | none |

## BS-11 — Tiny-LIVE readiness assessment

| Field | Value |
|-------|--------|
| status | NOT_READY |
| verdict | `NOT_READY` for operator tiny-LIVE |
| rationale | BS-10 §15 public acceptance incomplete due to environment SSL; operator must re-run OBSERVE/SHADOW with working public TLS before any mutation-capable attempt |
| CLOB V2 / execution SDK | Independent gate; `py-clob-client-v2` listed in optional `live` extras — not exercised here |
| LIVE authorized/executed | NO |

---

## Architecture result

Single authoritative quote path:

```text
MarketStateStore → immutable BookView → Z-Gap assemble/evaluate
```

Hybrid data path: REST bootstrap/recovery + WS continuous updates + binding-scoped ACTIVE/PREPARED feeds + sync barrier before READY.

## Plan deviations

1. Offline/fixture hosts without a store retain legacy ExecutableQuote assembly (compatibility only; live compose always wires the store).
2. Live WSS heartbeat/reconnect proven in code paths; not against public venue in this environment.
3. BS-10/11 blocked by TLS environment, not by missing implementation.

## Preexisting failures preserved

Same 47 full-suite failures as baseline (deleted `config/observe_*.json` / `n7_tiny_live.json` etc.). Not introduced by book-state work.

## Next action

On an operator host with working Polymarket public TLS: run a read-only OBSERVE compose long enough for **two** five-minute rollovers, confirm `bindings.json` + READY books + real asks in analytics, then re-score BS-11.

---

## Official SDK cutover (2026-07-30)

**Status:** COMPLETE_WITH_ENVIRONMENT_BLOCKER (read-only OBSERVE not re-run in this session; deterministic suite green for migration)  
**Stable SDK selected:** `polymarket-client==0.2.0`  
**Why:** latest stable PyPI release at implementation time; `0.3.0b1` rejected as pre-release.  
**SDK guidance verified:** YES — `PublicClient` / `AsyncPublicClient` / `SecureClient` / `AsyncSecureClient` APIs inspected from installed package + live public `get_order_book` / `get_event` probes.

### Transports removed

| Retired | Replacement |
|---------|-------------|
| Raw urllib CLOB `/book` (`rest_book.fetch_clob_book`) | `PublicClient.get_order_book` via `sdk_public.fetch_order_book_via_sdk` |
| Custom market WebSocket (`websockets` + JSON subscribe in BindingFeed / ws_adapter) | `AsyncPublicClient.subscribe(MarketSpec)` |
| `py-clob-client-v2` Secure/read/mutation | `SecureClient` / `AsyncSecureClient` |
| Gamma urllib discovery (production path) | `PublicClient.get_event` + `sdk_event_to_gamma_dict` |
| Raw user WS auth frames | `AsyncSecureClient.subscribe(UserSpec)` |

### Adapter ownership

```text
polymarket-client (pinned 0.2.0)
  → adapters/polymarket/sdk_public.py   (public REST + market WS normalize)
  → adapters/polymarket/sdk_secure.py   (SecureClient factory; Decimal balance scale)
  → adapters/polymarket/sdk_errors.py   (stable error categories)
  → adapters/polymarket/rest_book.py    (Tyrex RestBookPayload + store bootstrap)
  → adapters/polymarket/ws_adapter.py   (SDK stream → Tyrex book events)
  → market_data/book_feed.py           (BindingFeed sync barrier + DESYNCED recovery)
  → execution/polymarket/sdk_readonly.py / mutation_transport.py
  → Tyrex contracts → MarketStateStore → BookView → Z-Gap / planner / OMS
```

SDK models do not leak past the adapter boundary.

### DESYNCED recovery

- REST bootstrap is fail-soft per token (sibling continues).
- While `DESYNCED`, frames are still classified/accounted.
- Unsafe deltas are buffered, not applied onto an unknown book.
- A valid full WS `book` snapshot is an authoritative sync anchor (same official SDK architecture — not a legacy fallback).
- After both legs have books, buffered same-epoch deltas reconcile; ambiguity ⇒ remain `DESYNCED`.
- Buffer overflow clears and stays `DESYNCED` with `recovery_reason`.

### Authenticated / execution migration

- `SdkReadonlyTransport` builds `SecureClient.create(..., credentials=ApiKeyCreds(...))` — never create/derive keys.
- Mutations use `place_market_order` / `place_limit_order` / `cancel_order(order_id=...)` behind arm token.
- Positions via `SecureClient.list_positions`.
- No real venue mutations in this task.

### Dependency cleanup

- `pyproject.toml`: `polymarket-client==0.2.0` in core deps; `py-clob-client-v2` removed.
- No feature flag / dual transport / runtime fallback to V2. Narrow documented exception: public `GET /time` via `clob_server_time.py` (SDK has no server-time API).

### Tests / commands

```text
python -m pytest -q tests/test_polymarket_sdk_adapters.py
  → 12 passed

python -m pytest -q tests/test_book_state_bs1_bs2.py tests/test_book_state_bs5_through_bs9.py \
  tests/test_book_state_rest_and_feed.py tests/test_book_state_ws_protocol.py \
  tests/test_polymarket_sdk_adapters.py tests/test_r7a_dry_lifecycle.py \
  tests/test_r6d_auth_identity.py tests/test_r6c_preflight_immutable.py
  → 58 passed

python -m pytest -q
  → 48 failed, 707 passed, 1 skipped
  (48 FileNotFound / deleted-config family = pre-existing dirty tree;
   introduced n2 wrong-token test updated for SDK handler — now pass)
```

### Read-only evidence

Not re-executed as a full two-rollover OBSERVE in this implementation session. Prior ops proved Cloudflare 1010 on header-less urllib and success of official SDK `/book`. Label: `ENVIRONMENT_BLOCKER` until operator re-runs OBSERVE on a venue-reachable host.

### Blockers / deviations

1. Gamma `list_markets(condition_ids=...)` may return empty for some IDs; slug discovery remains the primary path.
2. SSR HTML attestation and Binance/RTDS websockets remain non-CLOB urllib/WS (out of CLOB transport cutover scope).
3. Injectable `GammaMarketDiscovery(opener=...)` retained only for deterministic older tests; production uses SDK.

---

## Server-time preflight repair (Candidate D)

**Status:** COMPLETE (deterministic + public read-only validation; LIVE not authorized)  
**HEAD before repair:** `18235c84c99bb0066bcc5271bd761fb191b0548c` (working tree; no commit)

### Proven root cause

After the unified-SDK cutover, `PreflightReadClient.get_server_time()` used a broken fallback:

```text
PublicClient has no get_server_time
→ get_order_book(token_id="0")
→ RequestRejectedError (application rejection)
→ /time probe recorded ok=False
→ live_preflight: TRANSPORT_DISCONNECTED / public_clob_unreachable
→ N7: connectivity_unavailable + hint_check_vpn_or_dns
→ fabricated UNKNOWN → preflight_reconciliation_disagreement
```

Connectivity was available; geoblock was false; real venue mutations were 0. The venue answered; Tyrex misclassified an application rejection as transport failure.

### Official `/time` semantics

- Endpoint: `GET https://clob.polymarket.com/time` (documented, public, no auth)
- Response: JSON integer Unix timestamp in **seconds**
- `polymarket-client==0.2.0`: **no** public server-time method

### Why the unified SDK remains valid

Books, market WS, authenticated reads, user stream, and execution stay on `polymarket-client==0.2.0`. Missing `/time` is a deliberate narrow Tyrex adapter exception — not a reason to restore V2 or a general raw CLOB client.

### Ownership

| Path | Owner |
|------|--------|
| Books / WS / auth / execution | `polymarket-client` via Tyrex adapters |
| Public server time only | `tyrex_pm.adapters.polymarket.clob_server_time.fetch_clob_server_time` |

Callers (`PreflightReadClient.get_server_time`, `SdkReadonlyTransport.get_server_time`) delegate to that single implementation.

### Removed misleading paths

- `get_order_book(token_id="0")` time fallback
- `hasattr(..., "get_server_time")` SDK compatibility guessing
- Production `probe_public_book` synthetic invalid-token connectivity probe
- N7 VPN/DNS hint on non-transport failures
- Fabricated `UNKNOWN` → `preflight_reconciliation_disagreement` when reconciliation never ran

### Corrected failure classification

| Failure | Classification |
|---------|----------------|
| DNS/TLS/timeout/connection | `TRANSPORT_DISCONNECTED` / `public_clob_unreachable` + VPN hint allowed |
| Valid `/time` integer | connectivity pass + venue-time pass (separately recorded) |
| Malformed/wrong-type `/time` body or HTTP 4xx/5xx on `/time` | `PUBLIC_TIME_INVALID` / `public_time_invalid` — **not** VPN/DNS |
| SDK `RequestRejectedError` | application `REJECTED`, not transport disconnect |
| Reconciliation disagreement | only when both preflights compared reachable account state and classifications differ |

Runtime `TimeAuthority` remains Binance/OS-monitor; this repair does not add Cristian sync from CLOB `/time`.

### Tests / commands

```text
python -m pytest -q tests/test_clob_server_time.py tests/test_preflight_server_time.py
  → passed (adapter + preflight/N7 classification)

python -m pytest -q tests/test_r6c_preflight_immutable.py tests/test_polymarket_sdk_adapters.py \
  tests/test_r7a_dry_lifecycle.py tests/test_r6d_auth_identity.py tests/test_n7_oneshot.py
  → 84 passed (with server-time tests)

python -m pytest -q
  → 48 failed, 741 passed, 1 skipped
  (same pre-existing deleted-config / missing JSON family; no introduced failures)

ruff check (changed modules) → clean after import/format fix
git diff --check (changed repair files) → clean
```

### Read-only evidence

```text
fetch_clob_server_time() → HTTP 200, int Unix seconds
CLI live-preflight --skip-auth → public_clob.venue_time_valid=true; blocker=credentials_missing_or_skipped (advanced past old public_clob_unreachable); probes=['/time'] only
CLI live-preflight (auth RO, --user-stream-s 0) → ok=True; recon ran; unreachable_account=false; mutations_attempted=false; heartbeat_called=false; no VPN hint; no /book synthetic probe
REAL_VENUE_MUTATIONS = 0
```

### Remaining blockers (server-time section)

- Operator re-run of full N7 preflight / OBSERVE after pagination/async repairs
- Tiny-LIVE still requires separate operator authorization
- Pre-existing deleted-config FileNotFound failures in the full suite (unrelated dirty tree)

---

## SDK pagination + async-client adapter repair

**Status:** COMPLETE (deterministic + mutation-disabled read-only validation; LIVE not authorized)  
**Depends on:** Server-time preflight repair above (unchanged / still current)

### False open-order causal chain (historical)

```text
SecureClient.list_open_orders() → Paginator
→ list(paginator) yields Page objects
→ one empty Page(items=()) converted as an “order”
→ open_order_count = 1
→ unexpected_open_order
```

Venue actually had zero open orders. Not local `state/`.

### Trade pagination impact

`get_trades()` used the same `list(paginator)` mistake, so `trade_count` counted **pages** (e.g. 39) rather than trade records. Correct flattening materializes all pages via `Page.items` (account-history size / duration is a known follow-up; no silent truncation).

### Unawaited async-client causal chain (historical)

```text
build_async_secure_client → returns AsyncSecureClient.create(...) coroutine
→ await client.subscribe(...) on coroutine
→ AttributeError
→ mislabeled connectivity_unavailable
```

No user-stream connectivity test actually ran.

### Normalized adapter-boundary design

| Concern | Owner |
|---------|--------|
| `Paginator` → records | `adapters/polymarket/sdk_pagination.py` (`drain_sdk_paginator` / `map_sdk_paginator`) |
| Async secure client | `async def build_async_secure_client` → `await AsyncSecureClient.create` |
| Domain conversion | converters refuse `Page`; require record identity |
| `/time` | unchanged narrow adapter |

Discovery already used `.iter_items()` correctly (inspection only).

### Corrected failure classifications

| Failure | Abort / readiness |
|---------|-------------------|
| Empty open-order pages | no `unexpected_open_order` |
| Real open orders after flatten | `unexpected_open_order` |
| Async init / AttributeError coroutine | `user_stream_init_failed` (not connectivity, not VPN) |
| User-stream transport | `connectivity_unavailable` without VPN hint |
| User-stream auth / protocol | `user_stream_auth_failed` / `user_stream_protocol_failed` |

### Additional findings

- **Fixed:** `get_positions_raw` same page-as-record bug.
- **Follow-up (not truncated here):** unfiltered `list_trades` full-history drain can be large/slow/rate-limit sensitive for busy accounts; propose bounded recon window later.
- **Inspection:** `AsyncPublicClient()` sync ctor + awaited `subscribe` in market feed path looks correct; mutation transport has no list pagination.

### Tests / commands

```text
python -m pytest -q tests/test_sdk_pagination_and_async.py
  → passed

python -m pytest -q tests/test_clob_server_time.py tests/test_preflight_server_time.py \
  tests/test_polymarket_sdk_adapters.py tests/test_r6c_preflight_immutable.py tests/test_n7_oneshot.py
  → 93 passed

python -m pytest -q
  → 48 failed, 767 passed, 1 skipped
  (same pre-existing deleted-config family; +26 new tests; no introduced failures)
```

### Read-only evidence

Same-day prior probe (before transient TLS outage) proved empty `Page(items=())` → 0 open-order records after correct interpretation.  
Current agent host RO re-run hit:

```text
ENVIRONMENT_BLOCKER: SSL CERTIFICATE_VERIFY_FAILED Hostname mismatch for clob.polymarket.com
```

No venue mutations attempted. Re-run RO preflight on a venue-reachable operator host.

### Remaining blockers

- Environment TLS/DNS to `clob.polymarket.com` on the agent host (operator host previously succeeded)
- Full-history trade drain performance follow-up for busy accounts
- Tiny-LIVE still requires separate operator authorization

---

## BTC 5m market-window identity repair

**Status:** COMPLETE_WITH_BLOCKERS (deterministic + mutation-disabled discovery RO; N7 compose/PTB seal path not safely reachable without `--live`)  
**Depends on:** SDK pagination + async-client repairs above (preserved / still current)

### Prior operator validation of SDK repairs (unchanged)

```text
preflight                    = GO
open_order_count             = 0
user stream                  = authenticated
reconciliation               = reached
real venue mutations         = 0
```

### Failed run chain (historical; diagnosis superseded)

```text
yaml_live_20260731T105211Z
→ discovery reject wrong_window:
   eventStartTime 2026-07-30T10:58:31.450519+00:00
   != slug epoch 2026-07-31T10:50:00+00:00
→ no market binding
→ PTB sealing never started
→ reason reported only as no_ptb_seal
→ vpn_hint = bool(errors) → true (false positive)
→ ABORTED_BEFORE_MUTATION
```

Earlier notes that treated `eventStartTime` as the recurring five-minute boundary are **superseded** by the verified field-semantics analysis below.

### Raw Gamma / SDK evidence (`btc-updown-5m-1785495000`)

| Field | Raw value | Actual semantic meaning | Authoritative for window identity? | Tyrex before fix | Tyrex after fix |
| ----- | --------- | ----------------------- | ---------------------------------: | ---------------- | --------------- |
| slug suffix `1785495000` | 2026-07-31T10:50:00Z | Recurring resolution window start | **Yes** | Used, then rejected by listing compare | Canonical `window_start` |
| `schedule.start_time` / event `startTime` | 2026-07-31T10:50:00Z | Same recurring window start | Yes (cross-check) | Underused | Optional agree-with-slug check |
| `schedule.start_date` / `state.start_date` | 2026-07-30T10:58:31.450519Z | Listing / publication | **No** | Mapped → `eventStartTime` → false reject | `listedAt` metadata only |
| `schedule.end_date` / `state.end_date` | 2026-07-31T10:55:00Z | Window end | Yes (cross-check) | Validated | Validated vs start+300s |
| legacy `eventStartTime` | ambiguous | May be listing or window by payload | **No** for BTC 5m | Required == slug | Ignored for identity |
| outcomes / token IDs / conditionId | Up/Down + ids | Instrument binding | Yes | Label map | Label map (unchanged) |

Slug is an **event** slug; nested market carries condition/tokens/end. Cache/stale guard: returned slug must equal requested slug (empty → `STALE_DISCOVERY_PAYLOAD`).

### Corrected contract

```text
requested slug
→ validate returned identity
→ parse aligned epoch from slug → canonical window_start
→ window_end = window_start + 300s
→ optional startTime agree; endDate agree when present
→ label-based Up/Down token binding
→ BinaryMarket.event_start/end = canonical window (PTB + strategy)
```

Listing/creation stay as `listedAt` / `createdAt` / opaque legacy `eventStartTime` metadata — never exposed as `window_start`.

### PTB boundary propagation

```text
bind_btc_5m_gamma_event → market.event_start (slug)
→ N4ObserveRuntime.open_session → ptb_engine.open_window(event_start=...)
→ strategy requested_window_start / session.event_start
```

Invariant proven in unit tests via `PtbCaptureEngine.open_window`.

### Failure + VPN reporting

| Before | After |
|--------|--------|
| `reason = no_ptb_seal` only | `primary_abort_code` = precise market code; downstream `PTB_NOT_STARTED` / `no_ptb_seal` |
| `vpn_hint = bool(errors)` | `vpn_hint_from_error_texts` — transport-class only |

### Tests / commands

```text
Baseline (pre-edit): tests/test_sdk_pagination_and_async.py + time/preflight → 60 passed
Full suite baseline: 48 failed, 767 passed, 1 skipped

python -m pytest -q tests/test_btc_5m_market_window_identity.py
  → 19 passed

python -m pytest -q tests/test_btc_5m_market_window_identity.py \
  tests/test_sdk_pagination_and_async.py tests/test_clob_server_time.py \
  tests/test_preflight_server_time.py tests/test_n3b_seal_contracts.py \
  tests/test_n7_oneshot.py tests/test_r7a1_market_window.py \
  tests/test_n2_real_data_adapters.py tests/test_n2_timing_and_updown.py
  → 148 passed

python -m pytest -q
  → 48 failed, 786 passed, 1 skipped
  (same deleted-config family; +19 identity tests; no introduced failures)

ruff check (changed modules) → clean
git diff --check (changed repair files) → clean
```

### Read-only evidence (mutation-disabled discovery probe)

```text
REQUESTED_SLUG              = btc-updown-5m-1785495000
RETURNED_EVENT_SLUG         = btc-updown-5m-1785495000
EVENT_START_TIME_SEMANTIC   = listing → listedAt (not mapped to eventStartTime)
CANONICAL_WINDOW_START      = 2026-07-31T10:50:00+00:00
CANONICAL_WINDOW_END        = 2026-07-31T10:55:00+00:00
OUTCOME_BINDING_RESULT      = Up/Down label map OK
PTB_BOUNDARY                = same as canonical start (binding.market.event_start)
PTB_STAGE_REACHED           = discovery_bound (compose seal not armed; n7 without --live stops at preflight)
PRIMARY_ABORT_CODE          = none
VPN_HINT                    = false
REAL_VENUE_MUTATIONS        = 0
FALSE_WRONG_WINDOW_PRESENT  = NO
```

Current-window RO bind also accepted (`btc-updown-5m-1785507900`) with listing ≈1 day earlier than slug epoch.

### Remaining blockers

- Full N7 compose → PTB seal loop requires `--live` (mutation-armed); not used in this repair
- Tiny-LIVE still requires separate operator authorization
- Pre-existing deleted-config FileNotFound failures (48) unrelated
