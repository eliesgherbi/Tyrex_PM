# Tyrex_PM LIVE Runtime Enhancement — Implementation Plan

**Document type:** Architecture review + implementation-ready plan (planning only; no production code in this task)  
**Branch context:** `rest_project` (working tree assessed, including uncommitted files)  
**Plan revision date:** 2026-08-08 (rev 2 — implementation-agent ready)  
**Evidence run:** `var/runs/z_gap/tiny_live_validation2` (`n7_20260808T185556Z`)  
**Installed SDK today:** `polymarket-client==0.2.0` (project `.venv`)  
**Latest stable SDK on PyPI (verified 2026-08-08):** `polymarket-client==0.5.0`  
**Official sources reviewed:** 2026-08-08 (see §8)

---

## 0. Concise executive decision

**Problem:** LIVE constructs authenticated mutation capacity *after* the entry candidate. In `tiny_live_validation2`, candidate → application dispatch was ~33.5 s; liquidity was gone; FAK returned certain no-match; session stayed OPEN → `MANUAL_INTERVENTION_REQUIRED`.

**Direction (unchanged):** Evolve N7/N6/`LiveOMS`. Do **not** create a parallel `LiveExecutionRuntime` engine. Prepare resources before strategy entry evaluation. Prefer official Polymarket SDK. Keep `LiveOMS` as sole order/lifecycle writer. User stream primary; REST backfill/recovery. SQLite deferred. Owner runs tiny-LIVE. No OBSERVE/SHADOW ladder required.

**Rev-2 corrections that change prior plan conclusions:**

1. **SDK:** Replace “no upgrade required” with a **compatibility decision gate**. Recommend **upgrade to 0.5.0 if the spike passes**, because 0.5.0 adds official order-market metadata caching (absent in 0.2.0). Do **not** build a custom metadata cache first.
2. **Clients/loops:** Prefer **one main asyncio loop** owning **one** `AsyncSecureClient` for UserSpec + HTTP. Never share one async client across loops/threads. All evidence enters `LiveOMS` via one serialized single-writer queue.
3. **Hot path:** `create_market_order` (sign) → final freshness gate on newest BookView → atomic durable pre-dispatch record → immediate `post_order`. Discard signed order if final gate fails. Do **not** use `place_market_order` on the hot path (it can auto-approve allowances).
4. **Allowance:** Readiness must prove sufficient allowance; insufficient blocks `ENTRY_ENABLED`. Hot path must not perform hidden ERC-20/ERC-1155 approvals.
5. **No-fill:** Distinct successful outcome `PASS_N7_NO_FILL` / `COMPLETED_NO_FILL` — never reuse `PASS_N7_SAFE_NO_ENTRY` after a mutation attempt.
6. **Phases:** Exact-scope durability + no-fill correctness move **before** another owner LIVE; book DESYNC recovery is required; CLOB-triggered DecisionScheduler is optional.

---

## 1. Executive summary

### Confirmed main architectural problem

Strategy entry evaluation runs while the mutation stack is cold. After `EnterIntent`, `_run_in_session_mutation_phase` builds SecureClient, readonly transport, N7 host, baseline, recovery, and user stream, then submits. Measured gap ≈ 33.5 s (`audit_events.jsonl`).

Secondary defects: Binance-throttled eval; weak/unused fresh gate; sync SecureClient on the asyncio loop; dedicated user-stream thread with a second `AsyncSecureClient`; 50 ms REST exit polling; clear FAK no-fill leaves blocking OPEN session; selected-leg scope persisted only after accept; synthetic latency marks; `place_market_order` can hide allowance mutations.

### Expected result

- Operator `--mode live --live` requests LIVE; preparation completes **before** entry eval.
- Capability matrix separates evaluate / entry-buy / exit-sell / reconcile / backfill.
- Internal path after candidate is ordered for SDK prep latency and final freshness.
- Truthful counters and spans; authoritative crash recovery from one durable pre-dispatch record.
- Certain FAK no-fill → resolved session + distinct successful terminal.

### Proposal disposition

**MODIFIED** (same as rev 1), with rev-2 hardening of SDK, ownership, hot path, allowance, capabilities, phases, and terminals.

### Most important design decisions

1. Pre-prepare (not “arm mutations”) before entry eval via existing `entry_eval_ready` hook.
2. Reuse `N7OneShotHost`, `N6LiveHost`, `LiveOMS`, `ExitSupervisor`, `n7_presubmit`, obligations, baseline, terminal classifier.
3. Five readiness states **plus** orthogonal capability bits.
4. Official SDK: prefer `AsyncSecureClient` + `create_market_order` + `post_order` + `UserSpec`; settlement waiter background-only.
5. SDK **0.5.0 upgrade preferred after compatibility spike** for `OrderMetadataCache`.
6. Defer SQLite until measured.
7. Distinct `PASS_N7_NO_FILL` / `COMPLETED_NO_FILL`.

---

## 2. Scope and non-goals

### Required before next owner tiny-LIVE

| ID | Requirement |
|----|-------------|
| R1 | Pre-signal preparation; `entry_eval_ready` false until capabilities allow entry eval |
| R2 | Truthful spans including real `http_post_started` (not “submit_start”) |
| R3 | Final freshness gate on newest BookView **after** create/sign, **before** POST |
| R4 | Atomic exact-scope + obligation + mutation-intent durability **before** POST |
| R5 | Distinct authoritative no-fill success terminal + session resolve |
| R6 | No inline settlement wait on submit return |
| R7 | User-stream primary; REST backoff/recovery only |
| R8 | Supervised user-stream reconnect + scoped backfill |
| R9 | Loop-owned async mutation; single-writer OMS boundary |
| R10 | Book DESYNC isolation + authoritative snapshot replace (required) |
| R11 | Allowance check at readiness; no hot-path auto-approval |
| R12 | SDK compatibility spike (0.2.0 vs 0.5.0) completed and recorded |

### Later / optional

- CLOB-triggered eval + DecisionScheduler coalescing
- Persistence/reporting optimization beyond one critical durable write
- Rollover PREPARED_NEXT beyond one-shot
- Pre-signed order reuse beyond single create→gate→post attempt

### Excluded

- Parallel LIVE engine / second OMS / second strategy
- OBSERVE/SHADOW ladder as LIVE gate
- Agent-operated tiny-LIVE or any venue/chain mutation during planning/implementation validation (owner only for LIVE)
- Custom metadata cache before evaluating SDK 0.5.0 cache

---

## 3. Current and target call graphs

### 3.1 Current production LIVE path (evidence-backed)

```text
CLI --mode live --live
  └─ n7_live_session.run_live_oneshot_session
       ├─ readonly preflight (may create/close clients)
       └─ run_live_zgap_compose  [public feeds; LiveOMS forbidden by design]
            ├─ EventDispatcher + MarketStateStore + BookFeedSupervisor
            ├─ Chainlink + Binance
            └─ N4ObserveRuntime
                 └─ Binance tick (≥1s) → EnterIntent
                      └─ compose 1s loop notices stop_entry_eval
                           └─ on_in_session_lifecycle
                                └─ _run_in_session_mutation_phase  ★ AFTER candidate
                                     ├─ SecureClient + SdkMutationTransport (sync)
                                     ├─ SdkReadonlyTransport (often 2nd client)
                                     ├─ N7OneShotHost / N6 / LiveOMS
                                     ├─ baseline + recovery
                                     ├─ user stream on dedicated thread + private loop
                                     │    (separate AsyncSecureClient)
                                     ├─ arm_operator_live
                                     ├─ try_enter → weak revalidate → LiveOMS.submit
                                     │    └─ place_market_order (prep+POST+optional allowance recovery
                                     │       + optional 30s settlement wait)
                                     ├─ exit supervision @ 50ms + REST ticks
                                     └─ terminal + reports
```

**Measured (`tiny_live_validation2`):**

| Event | UTC |
|-------|-----|
| ENTRY_CANDIDATE DOWN @ 0.73 | 18:55:56.177955Z |
| readonly attached | 18:56:16.853754Z |
| baseline sealed | 18:56:19.081827Z |
| user stream ready / armed | 18:56:29.619102Z |
| `command_submit_dispatch` | 18:56:29.634321Z |
| FAK reject (certain, matched=0) | 18:56:32.697533Z |

Outcome: `MANUAL_INTERVENTION_REQUIRED` / `recon_resolved_but_obligation_open`.

### 3.2 Target LIVE path

```text
CLI --mode live --live
  └─ run_live_oneshot_session
       ├─ seal config; operator_live_requested=true
       ├─ prepare_execution_resources()   # no orders, no approvals
       │    ├─ main-loop AsyncPublicClient feeds (or existing public adapters)
       │    ├─ main-loop AsyncSecureClient (single)
       │    ├─ UserSpec subscribe on same loop
       │    ├─ exact-scope recovery / baseline / allowance+balance proof
       │    ├─ warm SDK order metadata (0.5.0 cache) via create_market_order dry warm
       │    └─ capabilities → may_evaluate_entry when safe
       └─ run_live_zgap_compose(entry_eval_ready=capabilities.may_evaluate_entry)
            └─ on candidate:
                 risk → plan
                 → create_market_order (sign only; no POST)
                 → capture newest BookView + refs
                 → final_gate (edge/identity/age/cutoff/cap/sync/stream)
                 → if fail: discard SignedOrder; no POST
                 → atomic durable pre-dispatch record
                 → post_order immediately
                 → enqueue HTTP result to LiveOMS single-writer
                 → user events enqueue to same writer
                 → ExitSupervisor / settlement / terminal
```

### 3.3 Ownership boundaries

| Concern | Owner | Rule |
|---------|-------|------|
| Public books / BookView | `MarketStateStore` | sole book reducer |
| Strategy | `N4ObserveRuntime` / Z-Gap | no mutations |
| Risk / plan | N6 | no venue I/O |
| Readiness + capabilities | `N7OneShotHost` (evolve) | gates eval/submit |
| Order + obligations | `LiveOMS` | **only** writer of order/lifecycle state |
| Exit authorization | `ExitSupervisor` | woken by OMS facts |
| Settlement axes | shared settlement + fill ledger | matched ≠ confirmed |
| Terminal classification | `n7_terminal` + baseline recon | PASS authority |
| Crash-recovery authority | `LifecycleRuntimeStateStore` pre-dispatch record | reports derived |
| Reporting | `RunReporter` | derived; one critical journal write may be co-located |

### 3.4 Event-loop / thread ownership (mandatory)

**Preferred (default recommendation):**

| Resource | Owner |
|----------|-------|
| Main asyncio loop | compose + LIVE session |
| `AsyncSecureClient` | **that loop only** |
| UserSpec consume task | that loop |
| HTTP `create_market_order` / `post_order` / readonly | that loop |
| `LiveOMS`, `ExitSupervisor`, persistence, reporting, `asyncio.Event` | **main thread / main loop only** |
| Ingress | `asyncio.Queue` (or equivalent) serialized into LiveOMS single-writer task |

**Forbidden:**

- Sharing one `AsyncSecureClient` across threads or event loops (not proven safe in 0.2.0/0.5.0; do not assume).
- Stream-thread callbacks mutating LiveOMS / ExitSupervisor / readiness / persistence directly.
- Sync `SecureClient.place_market_order` on the main loop as the production path.

**Fallback only if spike proves same-loop subscribe+HTTP unsafe:**

- Client A on main loop for HTTP; Client B on dedicated stream loop.
- Each client owned by exactly one loop.
- Stream loop marshals normalized evidence via thread-safe queue to main-loop OMS writer.
- Never share clients; never cross-thread mutate OMS state.

**Current code (must retire):** `SdkAuthenticatedUserStream` creates a dedicated thread + private loop + its own `AsyncSecureClient` (`sdk_user_stream.py` ~L209–247), while mutation uses a separate sync `SecureClient`.

### 3.5 Clarifying “pre-arm” / preparation

These are **distinct**:

| Concept | Meaning | May mutate venue/chain? |
|---------|---------|-------------------------|
| `operator_live_requested` | CLI `--mode live` + `--live` | no |
| `resources_prepared` | clients constructed, feeds connected, host exists | no |
| `account_recovery_ready` | prior scope handled, baseline sealed, stream ready, allowance/balance OK | no (reads only) |
| `ENTRY_ENABLED` / `may_evaluate_entry` | strategy may emit candidates | no |
| `may_submit_entry_buy` | specific ENTRY_BUY may POST | yes, only via OMS path |
| `may_submit_exit_sell` | specific EXIT_SELL may POST | yes, inventory-reducing only |

**Preparation must not:** place orders, cancel orders, call allowance recovery / approval transactions, or otherwise mutate venue or chain state. Warm metadata using **read** paths and/or `create_market_order` that is **discarded** (never posted) is allowed only if create/sign performs no chain mutation (true in 0.2.0/0.5.0: signing is local; allowance recovery is only in `place_market_order` → `post_order_with_allowance_recovery`).

---

## 4. Confirmed findings (rev-2 status)

Prior §4 findings remain: problems 1–13 CONFIRMED or PARTIALLY CONFIRMED as in rev 1. Additive verifications:

| Topic | Verdict | Evidence |
|-------|---------|----------|
| SDK 0.2.0 has no order metadata cache | CONFIRMED | Installed package: no `OrderMetadataCache`; prep always `fetch_tick_size` / `fetch_neg_risk` / fee resolves |
| SDK 0.5.0 adds order metadata cache | CONFIRMED | Official Python changelog 0.5.0; wheel contains `AsyncOrderMetadataCache` / `SyncOrderMetadataCache`, TTL 10 min |
| `place_market_order` may auto-approve allowance | CONFIRMED | `post_order_with_allowance_recovery*` on allowance 400 |
| `create_market_order` + `post_order` bypasses auto recovery | CONFIRMED | `create_market_order` → sign only; `post_order` posts JSON only |
| `entry_eval_ready` unused by LIVE | CONFIRMED | wired in `shadow_continuous` only |
| Same BookView as plan evidence + revalidate | CONFIRMED | version-change gate inert |
| `feeds_alive` = task liveness | CONFIRMED | compose ~L657–662 |
| price_change rows include best_bid/best_ask in official/SDK models | CONFIRMED (docs/SDK); Tyrex currently ignores those fields when building deltas | Use as optional TOB cross-check; full book/price_change levels remain reconstruction authority |

---

## 5. Proposal challenge and decision table (rev-2)

| Topic | Prior plan | Rev-2 decision | Why |
|-------|------------|----------------|-----|
| SDK version | No upgrade | **Spike then prefer 0.5.0** | Official cache; 0.x may break; measure regressions |
| Custom metadata cache | Prewarm custom | **Forbidden until 0.5.0 rejected** | Prefer official facility |
| Async client topology | One AsyncSecureClient (underspecified) | **Loop-owned single client preferred; dual-client fallback with marshalling** | No cross-loop sharing proof |
| Hot path | validate → persist → place/create+post | **create/sign → final gate → persist → post_order** | Prep can stale the book; POST must be after newest gate |
| Allowance | Keep inside SDK place path | **Readiness gate + create/post hot path (no auto-approve)** | Hidden chain mutation forbidden |
| No-fill PASS | `NO_FILL_CONFIRMED` can_pass | **Distinct `PASS_N7_NO_FILL` / `COMPLETED_NO_FILL`** | Don’t overload flat/no-entry PASS |
| Scheduler | Combined with book fix | **Split: DESYNC required; scheduler optional** | Don’t block LIVE on coalescer |
| Prior-market recovery | Ambiguous auto vs MANUAL | **Auto exit-only only if full prior reconstruction proven; else MANUAL** | See §7.9 |
| SQLite | Deferred | **Still deferred** | Measure first |

---

## 6. Target architecture

### 6.1 Abstractions

| Name | Decision |
|------|----------|
| Parallel `LiveExecutionRuntime` engine | **Rejected** |
| Evolve `n7_live_session` + `N7OneShotHost` | **Yes** — preparation controller |
| `RuntimeReadinessState` (5 states) | **Yes** |
| Capability matrix | **Yes** (orthogonal) |
| `MarketExecutionContext` | Reuse feed supervisor active/prepared + persisted execution scope |
| `DecisionScheduler` | **Optional Phase 8** |
| `AsyncMutationPort` | Evolve transport: `acreate_signed_market_order` + `apost_signed_order` |
| OMS single-writer queue | **Required** |
| Lifecycle durable store | Authoritative crash recovery |
| `LatencyTrace` | Evolve `LatencyTimeline` |

### 6.2 Planes

```text
CONTROL: prepare resources + capabilities (no mutations)
HOT DATA: feeds → state → strategy → plan → create/sign → final gate → durable → post
LIFECYCLE: HTTP + UserSpec + REST backfill → LiveOMS writer → exit → terminal → derived reports
```

---

## 7. State machines and capability matrix

### 7.1 Readiness states (operator-visible)

1. `STARTING`
2. `CONNECTING`
3. `RECOVERING`
4. `EXECUTION_READY` — resources prepared; account/recovery/stream OK; **not** necessarily entry-enabled
5. `ENTRY_ENABLED` — may evaluate entry (see capabilities)

Preparation advancing through these states **does not** place orders or approve allowances.

### 7.2 Capability matrix (orthogonal)

| Capability | Meaning |
|------------|---------|
| `may_evaluate_entry` | Strategy may produce EnterIntent |
| `may_submit_entry_buy` | OMS may POST ENTRY_BUY |
| `may_submit_exit_sell` | OMS may POST EXIT_SELL for strategy-owned sellable inventory |
| `may_reconcile` | Readonly recon / obligation updates allowed |
| `may_backfill` | REST backfill after stream gap allowed |

**Typical combinations:**

| Situation | evaluate | entry BUY | exit SELL | reconcile | backfill |
|-----------|----------|-----------|-----------|-----------|----------|
| Flat + stream ready + books OK + allowance OK + window open | Y | Y | N | Y | Y |
| Flat + stream gap | N | N | N | Y | Y |
| Exposed + stream gap | N | N | Y* | Y | Y |
| Active-token book DESYNC | N | N | Y* if inventory sellable via bid evidence rules | Y | Y |
| Prior unresolved session (scope known, reconstruction incomplete) | N | N | N | Y | Y |
| Prior unresolved + full reconstruction ready | N | N | Y | Y | Y |
| Manual handoff required | N | N | N | Y (read-only evidence) | Y |
| Market rollover (one_shot) | N for new window | N | prior only | Y | Y |

\*Exit while stream gap: allowed only with fail-closed REST authority + inventory bounds; never invent fills.

### 7.3 Order submission states

`PLANNED → SIGNED_PENDING_GATE → GATE_REJECTED (discard) | PRE_DISPATCH_DURABLE → POSTING → {NO_FILL_CERTAIN | VENUE_ACCEPTED | AMBIGUOUS}`

### 7.4 Settlement / sellability / exit / user-stream

Unchanged intent from rev 1: matched ≠ confirmed; ExitSupervisor event-woken; user-stream supervised reconnect with backfill before ready; settlement waiter background-only; timeout ≠ uncertain dispatch.

### 7.5 Terminal outcomes (corrected)

| Outcome | After mutation attempt? | `ok` | Notes |
|---------|-------------------------|------|-------|
| `PASS_N7_SAFE_NO_ENTRY` / equivalent | **No** | true | Never after mutation attempt |
| `PASS_N7_NO_FILL` / `COMPLETED_NO_FILL` | Yes | **true** | Distinct success |
| `PASS_N7_ONE_SHOT_FLAT` / `FLAT_CONFIRMED` | Yes | true | Flat vs baseline |
| `UNKNOWN_RECONCILING` | maybe | false | |
| `MANUAL_INTERVENTION_REQUIRED` | maybe | false | |
| `RESIDUAL_EXPOSURE` / `EXIT_PENDING` | yes | false | |

#### `PASS_N7_NO_FILL` required report fields

- `ok=true`
- `mutation_attempts=1` (or exact count)
- `venue_acceptances=0`
- `entry_matched_quantity=0`
- `terminal_inventory=0`
- `authoritative_no_fill_proof=true` with proof payload below

#### Exact no-fill proof (all required)

1. Transport result `uncertain=false`
2. `ok=false` **or** explicit venue reject with zero match semantics
3. `venue_order_id` absent/null
4. `trade_ids` empty
5. cumulative matched quantity = 0
6. Error/classification is **FAK/FOK no-match / no resting liquidity** class (e.g. validation2 message), not generic 5xx/timeout
7. Selected-token balance equals baseline (scoped)
8. No owned open orders for submission attempt
9. Order + session obligations resolved for no-fill
10. Reconciliation agrees (no disagreement)

**Never** classify as confirmed no-fill: uncertain dispatch, network timeout after POST start, HTTP 5xx without body certainty, 425 exhausted ambiguity, partial decode failures.

### 7.6 Prior-market recovery vs manual handoff (contradiction resolved)

**Authoritative rule:**

1. On startup, load lifecycle store. If blocking prior session exists, read **persisted execution scope** (required).
2. If scope missing/corrupt → `MANUAL_INTERVENTION_REQUIRED` (no YES default).
3. If scope present **and** runtime can reconstruct **all** of:
   - prior market/condition/token identity
   - public book feed binding for that market (or REST-only exit path explicitly armed)
   - readonly reconciliation scoped to that token/condition
   - sellability + `may_submit_exit_sell`
   - ExitSupervisor path  
   then enter **automatic exit-only recovery** (`may_evaluate_entry=false`, `may_submit_entry_buy=false`).
4. If any reconstruction element is missing, **or** newly discovered market ≠ prior scope market → **manual handoff** (do not claim automatic recovery).
5. Never transfer unresolved prior exposure into a new market context.

Do **not** document “automatic prior-market recovery” unless step 3 is implemented and tested.

### 7.7 Atomic pre-dispatch record

**Single atomic durable record** written immediately before `post_order`, fail-closed if write fails.

**Minimum fields:**

- `session_id` / `run_id`
- `market_id`, `condition_id`
- `selected_outcome` (UP/DOWN/YES/NO as used), `token_id`
- `execution_role` (`ENTRY_BUY` / `EXIT_SELL`)
- `side`
- `amount` or `shares`
- `max_price` or `min_price` (and `max_spend` for BUY)
- `local_order_id`, `client_order_id`
- `intent_id`, `plan_id`, `submission_attempt_id`
- `baseline_reference` (seal id / baseline fingerprint)
- `obligation_state` (SUBMITTING)
- `mutation_intent` (fingerprint of signed order parameters; not private key material)
- timestamps / latency correlation ids

**Authoritative store for crash recovery:** `LifecycleRuntimeStateStore` (`var/runtime_state/n7/<run_id>/…`).  

**Reports / `audit_events.jsonl`:** derived evidence **unless** implementation explicitly elects the critical JSONL stream as the replay-authoritative journal (must be stated in code + tests). Default: lifecycle store authoritative; JSONL evidence.

**Crash semantics:**

| Crash point | Recovery |
|-------------|----------|
| Before durable commit | No venue mutation assumed; safe to re-prep; no ambiguous order |
| After durable commit, before POST | Treat as **ambiguous intent**; reconcile by scope before any retry; **no blind POST retry** |
| After POST, before local response apply | Ambiguous submission; REST + user stream recon; no blind retry |

---

## 8. SDK integration and upgrade gate

### 8.1 Sources (2026-08-08)

- https://docs.polymarket.com/dev-tooling/python  
- https://docs.polymarket.com/trading/place-orders  
- https://docs.polymarket.com/trading/manage-orders  
- https://docs.polymarket.com/trading/realtime-order-updates  
- https://docs.polymarket.com/concepts/order-lifecycle  
- https://docs.polymarket.com/market-data/websocket/market-channel  
- https://docs.polymarket.com/market-data/websocket/user-channel  
- https://docs.polymarket.com/api-reference/trading-rate-limits  
- https://docs.polymarket.com/changelog/sdks  
- https://github.com/Polymarket/py-sdk  
- https://pypi.org/project/polymarket-client/  
- Installed 0.2.0 source in `.venv`  
- Downloaded 0.5.0 wheel inspected under temp (not installed into project by this planning task)

### 8.2 Capability matrix

| Capability | Official SDK | Local today | Planned | Custom retained | Reason |
|------------|--------------|-------------|---------|-----------------|--------|
| Public streams | `AsyncPublicClient`, `MarketSpec` | adapters/BindingFeed | keep | BookView store | domain model |
| Refs | `CryptoPricesSpec` | adapters | keep | N4 | strategy |
| Auth | `AsyncSecureClient.create` | sync Secure + async stream client | **one loop-owned AsyncSecureClient** | wrappers | ownership |
| User stream | `UserSpec` | dedicated thread client | same-loop consume | normalize | OMS queue |
| Sign without post | `create_market_order` → `SignedOrder` | unused | **hot-path prep** | mapping | freshness order |
| Post only | `post_order` | unused | **hot-path POST** | mapping | **no allowance recovery** |
| Place combo | `place_market_order` | used | **avoid on hot path** | — | auto allowance recovery |
| Allowance read | `get_balance_allowance` | readonly transport | readiness gate | — | block ENTRY_ENABLED |
| Allowance mutate | recovery inside place helper | implicit risk today | **forbidden in hot path** | — | truthful mutations |
| Metadata cache | 0.5.0 `AsyncOrderMetadataCache` | absent in 0.2.0 | **use after upgrade** | none | official |
| Settlement wait | `wait_for_order_fill_settlement` | inline | background | map timeout | don’t block |
| Errors | `RequestRejectedError.retry_after` (improved 0.3.0+) | `sdk_errors` | keep + spike | classify | 425/429 |

### 8.3 SDK upgrade compatibility gate (Phase 1 deliverable)

**Pinned today:** `pyproject.toml` → `polymarket-client==0.2.0`.  
**Latest stable:** `0.5.0`.

**Recommendation:** **Upgrade to 0.5.0 contingent on spike PASS.**  
**Rationale:** Python 0.5.0 changelog: *“Order placement now caches market configuration and platform and builder fees…”* Verified in 0.5.0 wheel (`_internal/actions/orders/cache.py`, 10-minute TTL, single-flight). 0.2.0 has no equivalent. Prefer official cache over custom.

**Spike checklist (must record artifact under plan evidence path chosen by implementer, e.g. `Docs/implementation/runtim_enhancement/sdk_spike_0_5_0.md` — only if owner authorizes that extra doc; otherwise `var/` test artifact):**

1. Public API compatibility for symbols Tyrex imports  
2. Stream behavior: `MarketSpec`, `UserSpec`, `CryptoPricesSpec`  
3. Order response types (`AcceptedOrder` / `RejectedOrder`, `order_id` typing)  
4. Exceptions + `retry_after` on 429  
5. `wait_for_order_fill_settlement` signature/behavior  
6. Order metadata cache warm vs cold latency for `create_market_order`  
7. Confirm `post_order` still has **no** allowance recovery  
8. Tyrex unit/integration regression impact  

**Gate outcomes:**

- **PASS** → bump dependency to `==0.5.0`, rely on SDK cache; no custom cache.  
- **FAIL** → remain on 0.2.0; document blockers; **still** use create+post; optional **temporary** read-only prewarm via public `get_order_book` / tick endpoints only if necessary; revisit upgrade.

**Do not** install 0.5.0 into the project as part of planning-only work.

### 8.4 Allowance policy (final)

1. During `RECOVERING`→`EXECUTION_READY`, call public `get_balance_allowance` for collateral (and conditional token when relevant).  
2. If allowance insufficient for tiny-LIVE $5 path → **do not set** `may_evaluate_entry` / `may_submit_entry_buy`. Emit clear readiness blocker. Operator fixes allowances out-of-band (or a **separate, explicit, non-hot-path** approval tool — out of scope unless owner requests).  
3. Hot path uses `create_market_order` + `post_order` only → **no automatic approval**.  
4. Count every real order POST, cancel, approval, and chain mutation in counters/facts.  
5. Deterministic test: simulated insufficient-allowance reject on `post_order` must show **zero** approval calls.

---

## 9. Hot-path budget and ordering

### 9.1 Authoritative order flow (corrected)

```text
candidate / plan accepted
  → sdk_prep: create_market_order (sign; no POST)     # may use 0.5.0 metadata cache
  → capture newest BookView + reference state
  → final_gate: identity, sync health, ages, edge, cap, cutoff, stream capability
  → if fail: discard SignedOrder; stop
  → atomic durable pre-dispatch record (scope+obligation+intent)
  → http_post_started: post_order(signed)
  → response_received → enqueue to LiveOMS writer
  → user-stream evidence enqueue to same writer
```

**Maximum interval:** `final_gate_completed → http_post_started` target **≤ 5 ms P95** (persist + call setup only). If exceeded, treat as soft miss (warn) or hard reject if configurable threshold breached — default **warn + proceed** only if still within candidate/final-gate-to-POST age budget (§9.3).

### 9.2 Required measured spans

| Span | Notes |
|------|-------|
| `candidate_selected → sdk_prep_started` | |
| `sdk_prep_started → sdk_prep_completed` | create/sign only |
| `sdk_prep_completed → final_gate_completed` | includes fresh BookView capture |
| `final_gate_completed → durable_commit` | |
| `durable_commit → http_post_started` | must be tiny |
| `candidate_selected → http_post_started` | primary internal dispatch metric |
| `http_post_started → response_received` | external |

**Do not** use `submit_start` / `command_submit_dispatch` as a substitute for `http_post_started`.

### 9.3 Freshness dimensions (not one “book age”)

| Dimension | Meaning | Initial default (configurable) |
|-----------|---------|--------------------------------|
| WS/heartbeat liveness | transport alive | feed task + last RX watchdog |
| `last_market_msg_rx_age_ms` | last market message received | warn 2000 / hard 5000 |
| `last_valid_apply_age_ms` | last successful snapshot/delta apply | hard 2000 |
| `candidate_age_ms` | strategy candidate → now | hard **5000** (≠ ≤100 ms goal) |
| `reference_age_ms` | Binance/Chainlink freshness | align runtime config; tighten for LIVE |
| `final_gate_to_post_age_ms` | final gate → POST start | hard **50** |
| `sync_health` | OK/STALE/DESYNCED | DESYNC blocks entry |

**Clarify:** 5 s candidate TTL is a **safety ceiling**, not the performance target. Performance target after preparation: `candidate_selected → http_post_started` **≤ 100 ms P95 internal** (excluding venue RTT), with sdk_prep hopefully << that once 0.5.0 cache is warm.

### 9.4 Forbidden on hot path

- Client construction, baseline, recovery scans, discovery  
- User-stream connect  
- `place_market_order` (allowance recovery risk)  
- Settlement wait  
- Summary/manifest analytics rewrites  
- Allowance approval transactions  
- Blind retries after ambiguous POST  

### 9.5 Permitted

- `create_market_order` / local sign  
- BookView capture + pure final_gate  
- One atomic durable write  
- `post_order`  
- Enqueue facts to OMS writer  

---

## 10. File-by-file implementation plan

### Phase 1 — SDK compatibility decision + truthful telemetry

| File / artifact | Change |
|-----------------|--------|
| Spike env (temp venv; do not mutate prod blindly) | Install 0.5.0; run checklist §8.3 |
| `pyproject.toml` | Bump **only after spike PASS** |
| `n7_latency.py` | Nested spans; real http_post markers |
| `mutation_transport.py` | Split create/sign vs post timing hooks (even on 0.2.0) |
| `live_oms.py` / reporting | Counters: attempts, acceptances, no_fill, ambiguous, approvals=0 |
| Tests | Span ordering; marker ≠ dispatch wrapper |

### Phase 2 — Pre-signal preparation + event-loop ownership

| File | Change |
|------|--------|
| `n7_live_session.py` | `prepare_execution_resources()` before compose; submit path assumes prepared |
| `live_zgap_compose.py` | Wire LIVE `entry_eval_ready` to capabilities |
| `n7_oneshot_host.py` | Readiness states + capability matrix; no venue mutation in prepare |
| `sdk_user_stream.py` | Migrate to main-loop consumption; retire dedicated loop **or** implement dual-client marshalling fallback |
| `sdk_readonly.py` / mutation transport | Share loop-owned client |
| Tests | No OMS mutation from foreign thread; loop heartbeat during slow HTTP |

**Rollback:** must **not** silently re-enable lazy post-signal init. Legacy path only behind explicit `TYREX_ALLOW_LAZY_LIVE_INIT=1` (default unset/false) and abort if unset when unprepared.

### Phase 3 — Exact scope/recovery + atomic pre-dispatch persistence

| File | Change |
|------|--------|
| `n7_selected_leg.py` / host | Persist scope fields at durable pre-dispatch (not only after ACK) |
| `live_oms.py` | Atomic write of §7.7 record before POST |
| `lifecycle_state.py` | Schema for authoritative record; reject unscoped OPEN sessions |
| `n7_recovery.py` | Implement §7.6 rules; remove YES fallback |
| Tests | crash before/after durable; prior-token recovery; missing scope → MANUAL |

### Phase 4 — SDK prep + final freshness gate + async POST

| File | Change |
|------|--------|
| `mutation_transport.py` | `acreate_signed_market_order`, `apost_signed_order`; forbid place on LIVE hot path |
| `n7_presubmit.py` / N6/N7 | Final gate **after** sign, on newest BookView; discard on fail |
| Host try_enter | Implement §9.1 ordering |
| Tests | stale-during-sign discarded; final_gate_to_post span; warm/cold prep (0.2 vs 0.5) |

### Phase 5 — No-fill / ambiguous handling

| File | Change |
|------|--------|
| `execution_obligation.py` | Resolve session on certain no-fill |
| `live_oms.py` | Map certain zero-match reject → no-fill resolution |
| `n7_terminal.py` | `PASS_N7_NO_FILL` / `COMPLETED_NO_FILL`; never SAFE_NO_ENTRY after mutation |
| Tests | validation2-shaped reject; ambiguous ≠ no-fill; counters |

### Phase 6 — User-stream recovery + settlement + exit

| File | Change |
|------|--------|
| `sdk_user_stream.py` | Auto `reconnect_with_backoff` + capability flips |
| `mutation_transport.py` | Remove inline settlement wait; background helper |
| `exit_supervisor.py` / `n7_live_session.py` | Event wake; REST schedule ≠ 50 ms primary |
| Tests | disconnect while exposed; WS-before-HTTP; duplicate events; REST call budget |

### Phase 7 — Book DESYNC / snapshot recovery (required before LIVE)

| File | Change |
|------|--------|
| `book_store.py` | Crossed/invalid → token DESYNCED; no exception escape |
| `book_feed.py` | Reconnect only transport failures; DESYNC triggers bounded `get_order_book` replace |
| `sdk_public.py` | Optionally consume price_change best_bid/best_ask as cross-check; ignore custom TOB as authority |
| Tests | crossed delta DESYNC; snapshot restores; entry blocked while DESYNC |

### Phase 8 — Optional DecisionScheduler

| File | Change |
|------|--------|
| `live_zgap_compose.py` (+ optional helper) | CLOB-triggered eval + coalesce |
| Explicitly deferrable | Not required for first post-fix tiny-LIVE if Binance cadence + readiness + final gate suffice |

### Phase 9 — Persistence/reporting optimization (measured only)

| File | Change |
|------|--------|
| writer / OMS | Collapse redundant fsyncs only if Phase 1 shows material cost after Phases 2–5 |
| SQLite | Still deferred |

### Phase 10 — Deterministic integration + owner tiny-LIVE readiness

Full §13 tests; §14 checklist; owner command packet. Agent does not run LIVE.

---

## 11. Phased execution sequence and dependencies

| Phase | Objective | Depends on | Before owner LIVE? |
|------:|-----------|------------|--------------------|
| 1 | SDK spike + truthful telemetry | — | **Yes** (spike recorded) |
| 2 | Preparation + loop/client ownership | 1 preferred | **Yes** |
| 3 | Exact scope + atomic pre-dispatch | 2 | **Yes** |
| 4 | create/sign → final gate → post | 1–3 | **Yes** |
| 5 | No-fill / ambiguous | 3–4 | **Yes** |
| 6 | Stream recovery + settlement + exit | 2,4 | **Yes** |
| 7 | Book DESYNC/snapshot | — (can parallel) | **Yes** |
| 8 | DecisionScheduler | 7 optional | **No** |
| 9 | Persistence trim | measured need | **No** |
| 10 | Integration + readiness packet | 1–7 | **Yes** |

**Deviation from user’s approximate list:** Phase 8 scheduler deferred; Phase 7 book correctness required (user item 1 under I). Telemetry merged with SDK spike as Phase 1.

**Rollback rule:** Feature flags may disable optional scheduler or reporting trim, but **must not** re-enable post-candidate lazy init unless explicit env override is set; default fail-closed abort if unprepared.

---

## 12. Migration and compatibility

- Authoritative path: prepare → gated compose → create/sign → final gate → durable → post → OMS writer.  
- Obsolete: post-intent client/baseline/stream init; sync place on loop; YES fallback; 50 ms REST primary; inline settlement wait; SAFE_NO_ENTRY after mutation.  
- Temporary: dual-client stream fallback; `to_thread` only if async port incomplete (**still** single-writer marshalling).  
- Lifecycle schema bump: unscoped blocking sessions → MANUAL.  
- N4/N6/N7 retained; no second engine.

---

## 13. Validation plan (deterministic)

### Required race / crash / ownership tests

1. User-stream fill arrives before HTTP response → single correct OMS state  
2. Duplicate user events → idempotent apply  
3. No OMS/ExitSupervisor/persistence mutation from stream thread  
4. Event-loop heartbeat advances during slow HTTP POST  
5. Stream disconnect while exposure open → entry blocked, exit/recon capabilities per matrix  
6. Book/reference stale during create/sign → final gate fails; signed order discarded; **no POST**  
7. Final-gate failure discards signed order  
8. Insufficient allowance: readiness blocks entry; post path cannot invoke approval helper  
9. Crash before durable commit  
10. Crash after durable commit before POST → ambiguous recon, no blind retry  
11. Crash after POST before response apply → ambiguous recon  
12. Exact prior-token recovery (and MANUAL when incomplete)  
13. No-fill counters (`ok=true`, attempts=1, acceptances=0, matched=0)  
14. SDK 0.2.0 vs 0.5.0 warm/cold `create_market_order` metadata timing (spike artifact)  

### Additional

- Crossed book DESYNC + snapshot restore  
- Partial fill; delayed accept; ambiguous transport  
- Filled BUY → sellable → SELL → flat  
- Reporting consistency / latency span ordering  
- REST call count under threshold on happy path  

**Owner-operated:** real tiny-LIVE after §14. Agent must not execute it.

---

## 14. Tiny-LIVE readiness checklist

- [ ] SDK spike recorded; dependency decided (0.5.0 PASS or explicit 0.2.0 remain)  
- [ ] Preparation completes before any candidate; lazy init disabled by default  
- [ ] Zero client construction / baseline / recovery after candidate  
- [ ] Loop ownership model implemented + tests green  
- [ ] Capability matrix enforced (gap/DESYNC block entry, not necessarily exit)  
- [ ] Hot path order = create/sign → final gate → durable → post  
- [ ] `http_post_started` truthfully marked  
- [ ] Allowance insufficient cannot hot-path approve  
- [ ] Exact scope durable before POST; no YES fallback  
- [ ] Certain no-fill → `PASS_N7_NO_FILL` with required fields  
- [ ] Ambiguous ≠ no-fill  
- [ ] No 50 ms REST primary; no inline settlement wait  
- [ ] Book DESYNC recovery tests green  
- [ ] Race/crash tests §13 green  
- [ ] Owner receives exact command + inspect paths  

### Suggested owner command (confirm against CLI at implementation time)

```text
# Owner only — not executed by implementation agent
tyrex-pm ... --mode live --live   # sealed tiny $5 configs
# Inspect:
#   var/runs/z_gap/<run>/audit_events.jsonl
#   var/runs/z_gap/<run>/attachments/operator_outcome.json
#   var/runtime_state/n7/<run_id>/lifecycle_state.json
```

---

## 15. Unresolved owner decisions (with recommended defaults)

| ID | Issue | Options | **Recommended default** | Evidence that would change it |
|----|-------|---------|-------------------------|-------------------------------|
| D1 | Upgrade to 0.5.0 | upgrade / stay 0.2.0 | **Upgrade if spike PASS** | Spike FAIL on API/stream/regression |
| D2 | Same-loop UserSpec+HTTP vs dual client | single / dual+marshal | **Single loop-owned client** | Proven races under load |
| D3 | Candidate TTL | 5s / 2s / other | **5s hard ceiling**; optimize for ≤100 ms dispatch | Live miss rate vs reject rate |
| D4 | `final_gate_to_post` hard vs warn | hard reject / warn | **Warn at 5ms; hard at 50ms age** | Persist latency distribution |
| D5 | Auto prior-market exit-only | full auto / MANUAL unless complete | **Auto only if §7.6 step 3 complete; else MANUAL** | Reconstruction cost |
| D6 | No-fill display name | `PASS_N7_NO_FILL` vs `COMPLETED_NO_FILL` | **`PASS_N7_NO_FILL`** with `COMPLETED_NO_FILL` alias ok | Operator preference |
| D7 | Enable price_change TOB cross-check | on / off | **On as cross-check after Phase 7** | False DESYNC rate |
| D8 | SQLite | now / later | **Later** | Critical write P95 after Phase 4 |
| D9 | Out-of-band allowance tooling | none / explicit CLI | **None in hot path; operator fixes manually for tiny-LIVE** | Repeated allowance blockers |

---

## 16. Final implementation checklist

1. Run SDK 0.2.0 vs 0.5.0 spike; record gate; bump pin only on PASS.  
2. Add truthful spans/counters (`http_post_started` real).  
3. Implement readiness states + capability matrix.  
4. Prepare resources before compose; wire `entry_eval_ready`.  
5. Migrate to loop-owned `AsyncSecureClient` + OMS single-writer queue.  
6. Atomic pre-dispatch record + exact scope; remove YES fallback.  
7. Implement create/sign → final gate → durable → `post_order`.  
8. Ban hot-path `place_market_order` / allowance recovery.  
9. Certain no-fill → resolve session + `PASS_N7_NO_FILL`.  
10. Ambiguous path fail-closed; no blind retry.  
11. User-stream auto reconnect + capability flips; background settlement.  
12. Replace 50 ms REST primary with event wake + backoff.  
13. Book DESYNC + snapshot replace; no reconnect on local inconsistency.  
14. Optional scheduler only after required phases.  
15. Persistence trim only if measured.  
16. Green §13 tests.  
17. Produce §14 packet.  
18. **Stop for owner tiny-LIVE.**

---

## Appendix A — Working-tree note

Plan assesses current working tree (including uncommitted lifecycle work), not HEAD alone.

## Appendix B — Strengths to preserve

Z-Gap model; strategy/risk/planning separation; MarketStateStore/BookView; selected-leg type; ENTRY_BUY/EXIT_SELL roles; obligations; baseline terminal recon; ExitSupervisor; matched≠confirmed; $5 fee-inclusive cap; FAK; official SDK boundary; fail-closed ambiguous submission.

## Appendix C — Non-actions of this planning revision

- No production code implemented  
- No configuration changed  
- No tests changed  
- No OBSERVE/SHADOW/LIVE run executed  
- No venue or blockchain mutation performed  
- No other documentation updated  
- SDK 0.5.0 wheel downloaded for inspection only; **not** installed into the project environment

