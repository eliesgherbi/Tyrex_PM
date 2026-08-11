# Polymarket integration currency audit

**Audit date:** 2026-07-30  
**Branch:** `rest_project`  
**HEAD:** `18235c84c99bb0066bcc5271bd761fb191b0548c`  
**Scope:** audit and recommendation only — no code changes, no order mutations  

**Relevant runs reviewed:**

- `var/runs/z_gap/yaml_live_20260730T062958Z/`
- `var/runs/z_gap/yaml_live_20260730T082212Z/`

**Prior task report:** `Docs/implementation/book_state_task/implementation_report.md`
**Prior plan:** `Docs/implementation/book_state_task/impelmentation_plan.md`

---

## 1. Executive verdict

The last book-state task was **conceptually correct**. It did **not** invent Cloudflare blocking; it **introduced a new mandatory REST bootstrap** implemented with **header-less urllib**, which Cloudflare rejects with **Error 1010**. That failure, combined with a **non-resilient DESYNCED policy that drops WebSocket events**, produces empty books.

**Smallest clean restore path:**

1. Keep direct REST `/book` (Option A) with an **honest Tyrex User-Agent** (not browser impersonation), retries, and error classification.  
2. Separately fix BindingFeed recovery so a temporary REST failure cannot permanently darken a feed that can receive official WS `book` snapshots.  
3. Defer unified `polymarket-client` migration for market WS and execution until after the read-only book path is proven green.

---

## 2. Last-task reconstruction

### Intent

Repair Polymarket book-state end-to-end (BS-1…BS-11): binding identity, `MarketStateStore`, REST bootstrap, binding-scoped feeds, immutable `BookView`, Z-Gap quote path, rollover, readiness reasons, pre-submit revalidation.

### Before

```text
N7 compose:
  Gamma discovery → PolymarketMarketWsAdapter (ACTIVE only)
  → BookSnapshotReceived → _apply_book(session tops)
  → BookDeltaReceived → count only / discard
  → prepare_aligned_eval uses MarketSession.up_*/down_*
  → no REST bootstrap, MarketStateStore bypassed on N7 path
```

July 27 failure: many CLOB events counted; strategy quotes still null.

### After (current)

```text
N7 compose:
  BookFeedSupervisor(ACTIVE + PREPARED_NEXT)
  → BindingFeed: WS connect + subscribe + buffer
  → REST /book bootstrap (required for sync barrier)
  → reconcile → READY
  → MarketStateStore → BookView → Z-Gap
  session tops = read-only projections only
```

### What passed / failed

| Gate | Result |
|------|--------|
| Deterministic BS-1…BS-9 tests | PASS |
| Full suite newly introduced failures | none (47 pre-existing config FileNotFound remain) |
| Public OBSERVE/SHADOW acceptance | ENVIRONMENT_BLOCKER then operational FAIL on REST 403 |
| Tiny-LIVE readiness | NOT_READY |

### Did the task introduce the incident?

| Claim | Assessment |
|-------|------------|
| Task introduced Cloudflare 1010 as a venue policy | No — venue/WAF behavior |
| Task introduced the failing client | **Yes** — new `rest_book.fetch_clob_book` with no User-Agent |
| Task made REST mandatory for READY | **Yes** — sync barrier design |
| Task made DESYNCED drop WS updates | **Yes** — BindingFeed `_handle_raw` only buffers/publishes in selected phases |
| Unrelated domain redesign | No — Z-Gap math/thresholds/OMS architecture preserved |

---

## 3. Current incident — proven root cause

### Causal chain (validated)

```text
minimal urllib /book (Accept only, no User-Agent)
→ Cloudflare 1010 / HTTP 403
→ REST bootstrap exception
→ BindingFeed phase DESYNCED (and reconcile fails without snapshot)
→ WS events not published to dispatcher while DESYNCED
→ clob_book_events = 0; snapshots/deltas applied = 0
→ no READY BookView
→ up_ask/down_ask null in analytics
→ BOOK_FEE_READY never evaluated; JUMP_GUARD / MODEL_NOT_READY dominate
```

### Proven by logs and probes

1. Run `082212Z`: `active_feed_phase=DESYNCED`, `clob_book_events=0`, 250 null asks, console `rest_book_http_403`.  
2. Geoblock `blocked:false` (CH/ZH) — not country block.  
3. Probe body for header-less request: Cloudflare JSON Error **1010** (“blocked access based on your browser's signature”).  
4. Same token with honest UA `tyrex-pm/0.3 …` → **200** with real book.  
5. `py_clob_client_v2.get_order_book` → **200** (SDK sets `User-Agent: py_clob_client_v2`).  
6. `polymarket.PublicClient.get_order_book` (unified SDK 0.2.0) → **200**, comparable tops/depth.  
7. Live market WS probe (8s): received `book` and `price_change`; Tyrex normalizer parsed snapshots/deltas.

### Strongly inferred

- During operator runs, WS likely connected (`reconnect_count ≥ 1`) and could have received frames, but nothing was applied because DESYNCED drops non-READY traffic.  
- First-token REST failure aborts bootstrap loop early → sibling DOWN token never attempted (metrics show UP-only tokens).

### Still unproven

- Exact live-run WS wire capture (no bounded raw capture enabled).  
- Whether Cloudflare would also block other header-less hosts differently.  
- Whether Error 1010 policy is permanent or can change without notice.

### Why zero WebSocket events were applied

Code-level fact in `BindingFeed._handle_raw`:

- Buffer only in `WS_BUFFERING | SNAPSHOT_ACQUIRING | RECONCILING`
- Publish only in `READY`
- **`DESYNCED` → silent drop**

Additional bug: after `bootstrap_rest` fails and sets `DESYNCED`, `_run` still assigns `self.phase = RECONCILING`, then reconcile finds no books → `DESYNCED` again, then the live reader continues dropping.

Also: compose `clob_book_events` increments only on dispatcher publish; dropped WS frames never appear there.

---

## 4. Latest official Polymarket integration baseline

**Access date:** 2026-07-30

| Package | Latest observed | Installed in this env | Notes |
|---------|-----------------|-----------------------|-------|
| `polymarket-client` | **0.2.0** stable; also **0.3.0b1** pre-release | temporarily installed for probe only | Official unified Python SDK; Py≥3.11; import package `polymarket` |
| `py-clob-client-v2` | **1.1.0** on PyPI | **1.0.1** installed | Transitional CLOB V2 client; repo README points new projects to py-sdk |
| `py-clob-client` (v1) | archived | not installed | Archived May 2026; no active Tyrex imports found |

Sources reviewed (primary):

- https://docs.polymarket.com/getting-started/sdks-apis  
- https://docs.polymarket.com/getting-started/python / https://docs.polymarket.com/dev-tooling/python  
- https://docs.polymarket.com/getting-started/migrate-from-previous-sdks  
- https://docs.polymarket.com/market-data/prices-order-books  
- https://docs.polymarket.com/market-data/websocket/market-channel  
- https://pypi.org/project/polymarket-client/  
- https://pypi.org/project/py-clob-client-v2/  
- https://github.com/Polymarket/py-sdk  
- https://github.com/Polymarket/py-clob-client-v2  

Official facts used:

- REST `GET /book?token_id=` remains documented; bids ascending / asks descending (best last); includes hash/tick/min size.  
- Market WS URL `wss://ws-subscriptions-clob.polymarket.com/ws/market`; subscribe `{assets_ids, type:"market"}`; app `PING` every 10s; events include `book`, `price_change` (size 0 deletes level), `tick_size_change`.  
- Direct API use remains legitimate; SDK is recommended convenience, not a ban on custom adapters.  
- Migration guidance: replace `py-clob-client-v2` with `polymarket-client` for new work.

---

## 5. Active Polymarket touchpoint matrix

| Area | Active code path | Current approach | Latest official approach | Status | Evidence | Risk | Recommended action |
|------|------------------|------------------|--------------------------|--------|----------|------|--------------------|
| REST book bootstrap | `adapters/polymarket/rest_book.py` | urllib, Accept only | `/book` + honest client identity / SDK | **INCOMPATIBLE** (ops) | CF 1010 probes; live 403 | Blocks READY books | Option A UA+retry NOW |
| Market WS feed | `market_data/book_feed.py` | websockets + normalize + store | same endpoint/frames; SDK optional | **CURRENT_BUT_FRAGILE** | DESYNCED drops events | Dark books after REST fail | Recovery redesign NOW (separate) |
| Legacy WS adapter | `adapters/polymarket/ws_adapter.py` | same URL; library ping historically | app PING required | **CURRENT_BUT_FRAGILE** | BindingFeed now primary for N7 | Dual adapters confuse ownership | Keep for non-N7 hosts; don’t dual-write quotes |
| Normalize | `adapters/polymarket/normalize.py` | book/delta/tick | official event types | **CURRENT_AND_CORRECT** | WS probe parse ok | Low | Keep |
| Discovery | `adapters/polymarket/discovery.py` | Gamma + Tyrex UA | Gamma/events APIs | **CURRENT_AND_CORRECT** | live discovery works | Low | Keep |
| Store/View | `book_store.py`, `book_view.py` | Tyrex contracts | n/a (domain) | **CURRENT_AND_CORRECT** | unit tests | Low | Keep |
| Compose quote path | `live_zgap_compose.py`, `n4_observe_runtime.py` | Store→BookView | n/a | **CURRENT_AND_CORRECT** (design) | code/tests | Blocked by feed health | Keep boundary |
| Public CLOB reads (R6/R7) | `preflight_client.py`, `r7b_live_once.py`, etc. | urllib **with** Tyrex UA | `/book` etc. | **CURRENT_AND_CORRECT** | UA probes pass | Low | Keep pattern; align rest_book |
| Readonly SDK | `sdk_readonly.py` | `py_clob_client_v2` | migrate to unified later | **OUTDATED** (transitional) | docs migrate page | Medium maintenance | Plan migration NEXT |
| Mutation transport | `mutation_transport.py` | armed `py_clob_client_v2` FAK/limit | V2/unified order APIs | **CURRENT_BUT_FRAGILE** | uses `float(...)` | Precision/duplicate risk | Decimal hygiene BEFORE LIVE; SDK migrate later |
| L2 HMAC | `l2_hmac.py` | POLY_* headers | still documented for CLOB | **CURRENT_AND_CORRECT** / verify on migrate | code | Medium if contracts change | Verify on unified migration |
| User WS readonly | `user_stream_readonly.py` | documented user WS URL | same | **UNVERIFIED** in this audit | URL match only | Medium | Smoke before LIVE |
| Fees | domain fee curve + `fees_fd.py` | local phi curve | venue fee schedule may change | **CURRENT_BUT_FRAGILE** | not re-fetched live | Economic mis-size | Confirm fee source BEFORE LIVE |
| Dep pin | `pyproject.toml` live extra `py-clob-client-v2>=1.0.0` | allows 1.0.1 | latest 1.1.0 | **CURRENT_BUT_FRAGILE** | PyPI vs installed | Drift | Bump/test before LIVE |
| Unified SDK | not in project deps | — | recommended new baseline | **UNVERIFIED** for Tyrex exec | probe REST only | High if rushed | Optional REST helper NEXT; exec later |
| V1 SDK | none in `src/` | — | archived | **DEAD_CODE** absent | grep clean | None | Keep banned |
| `old/` | archived tree | SDK bootstrap + WS primary | historical | **DEAD_CODE** | `old/` | Confusion only | Do not restore wholesale |

---

## 6. Unified SDK compatibility matrix (Tyrex needs)

| Capability | Classification | Notes |
|------------|----------------|-------|
| Market/event discovery | Supported and verified (API present) | `PublicClient` gamma methods exist |
| Single order book | Supported and verified | probe OK; venue bid/ask order still best-last |
| Batch books | Supported (API present) | not probed |
| Book hash/tick/min size | Supported and verified | present on `OrderBook` |
| Market WS subscribe | Supported but unverified in Tyrex harness | docs + SDK streams; not adopted |
| `book` / `price_change` | Supported (docs/SDK) | Tyrex custom WS already works |
| Heartbeat | Supported (docs) | BindingFeed already sends PING |
| Typed errors | Supported | useful for 403/429 classification |
| Signer vs funder / wallet types | Supported but unverified for Tyrex envelopes | migration docs emphasize SignatureTypeV2 |
| API creds derive | Supported | already via v2 today |
| Limit / market / FAK/FOK | Supported | must re-validate tiny-live guards |
| max/min price protection | Supported but unverified in unified helpers | Tyrex must keep own checks |
| Cancel / open orders / trades | Supported | adapter boundary required |
| User WS | Supported but unverified | |
| Better retained direct | BindingFeed sync machine, MarketStateStore, BookView, OMS arming | do not leak SDK types upward |

**Conclusion:** unified SDK is viable for **public REST** and eventually trading, but it is **not** required to fix the current outage, and it is **not** yet proven against Tyrex tiny-live safety invariants.

---

## 7. Solution comparison for `/book` failure

| Criterion | A Direct REST + honest UA | B SDK REST bootstrap only | C SDK REST+market WS |
|-----------|---------------------------|---------------------------|----------------------|
| Official support | Yes (documented `/book`) | Yes (v2 or unified) | Yes |
| Reliability | High if UA+retry | High (known-good UA/session) | High, larger blast radius |
| Type safety | Manual | Better | Best |
| Retry/session | Must add | Often included | Included |
| REST/WS consistency | Tyrex owns both normalizers | Must map SDK book→Tyrex snapshot | Must map SDK stream→Tyrex events |
| Latency | Lowest control | Fine for bootstrap | Fine |
| Dependency impact | None | Optional live/SDK dep on read path | Large |
| Integration effort | Small | Medium | Large |
| Testability | Easy fixtures | Need SDK mocks/fixtures | Harder |
| Migration risk | Low | Medium | High |
| Future maintenance | Must watch WAF/headers | Follows vendor client | Follows vendor, less Tyrex control |

### Recommendation

**Option A now (permanent, not temporary hack):** honest application User-Agent + Accept + classified retries.

**Not** Chrome impersonation.

**Option B** as optional NEXT hardening (prefer `PublicClient.get_order_book` or existing v2 only inside adapter).

**Option C** deferred until BindingFeed ownership and tiny-live execution migration are planned deliberately.

Preserve boundary:

```text
Polymarket API/SDK
→ Tyrex polymarket adapter
→ snapshot/delta contracts
→ MarketStateStore
→ BookView
→ strategy
```

---

## 8. DESYNCED / recovery assessment (separate from HTTP fix)

| Question | Assessment |
|----------|------------|
| Must REST succeed before any WS snapshot? | **No.** Official WS `book` is a full snapshot. REST-required READY is Tyrex policy that is currently too brittle. |
| Can WS `book` establish coherent state? | **Yes** (official docs + probe). |
| Can `price_change` arrive before snapshot? | **Yes** — buffering is correct; applying deltas without snapshot is not. |
| Hash/sequence gap detection | Hash is diagnostic only (plan already said this); no venue sequence assumed. |
| Retry/backoff | Present for WS reconnect; REST bootstrap lacks classified retry. |
| Recovery after 403/429/5xx | Currently poor — stays dark while connected. |
| One failed token darkens both? | **Yes** — bootstrap returns on first exception. |
| Recovery without host restart? | Intended via reconnect loop, but DESYNCED drop prevents healing on a live socket. |

**Recommended separate changes:**

1. On REST failure: remain SYNCING/DESYNCED but **accept first valid WS `book` as authoritative snapshot**, then reconcile buffered deltas.  
2. While DESYNCED/SYNCING after connect: do not silently drop; buffer or accept snapshots.  
3. Do not overwrite `DESYNCED` with `RECONCILING` after failed bootstrap.  
4. Fail-soft per token; continue sibling token.  
5. Increment REST failure metrics even when fetch raises before apply.  
6. Keep HTTP-client fix and state-machine fix as **separately testable PRs**.

---

## 9. Other outdated / fragile usage

| Finding | File/symbol | Severity | Blocks next live? | Correction |
|---------|-------------|----------|-------------------|------------|
| Header-less `/book` | `rest_book.fetch_clob_book` | **Critical** | **Yes** | Honest UA + retries |
| DESYNCED drops WS | `BindingFeed._handle_raw` | **Critical** | **Yes** (resilience) | Accept WS book / buffer |
| Bootstrap abort both legs | `BindingFeed.bootstrap_rest` | High | Yes for half-books | Per-token fail-soft |
| `float()` order sizing/price | `SdkMutationTransport.submit_order` | High | Safety | Decimal end-to-end |
| v2 pin lag 1.0.1 vs 1.1.0 | env / `pyproject` live extra | Medium | Should before LIVE | Upgrade + regression |
| Unified SDK not adopted | deps | Medium (future) | No for book restore | Planned migration |
| Dual market WS implementations | `ws_adapter` vs `book_feed` | Medium | No if N7 uses supervisor only | Document ownership |
| Fee curve local vs venue | strategy/fees | Medium | Before LIVE assert | Confirm schedule |
| User stream lightly audited | `user_stream_readonly.py` | Medium | Before LIVE | Smoke test |
| Deleted fixture configs | `config/observe_*.json` | Low for book | No | Restore or fix tests (pre-existing) |

### Correct components to leave alone

- `MarketStateStore` / `BookView` / binding identity model  
- Z-Gap valuation thresholds / PTB math  
- OMS arming / mutation gate design  
- Discovery outcome semantics validation  
- Normalize size-0 delete semantics  
- Planner revalidation concept (BS-9)

---

## 10. Prioritized modernization plan

### NOW — restore and prove read-only book path

1. **Honest REST client identity** (`rest_book.py`)  
   - Scope: User-Agent `tyrex-pm/<version> (read-only market-data)`, Accept, retry on 429/5xx, classify 403/1010.  
   - Deps: none. Risk: low.  
   - Tests: unit with mocked HTTP; live probe both UP/DOWN.  
   - Acceptance: compose `clob_ready` true or SYNCING→READY; non-null asks when venue has asks.

2. **DESYNCED recovery** (`book_feed.py`) — separate PR  
   - Scope: WS `book` can bootstrap; don’t drop while recovering; per-token fail-soft; don’t clobber DESYNCED.  
   - Tests: REST fail then WS book → READY; one token REST fail doesn’t kill sibling.  
   - Acceptance: temporary REST 403 does not yield permanent null quotes if WS books arrive.

### BEFORE NEXT TINY-LIVE

3. Confirm fee inputs and FAK BUY amount/fee-inclusive cap still match venue.  
4. Replace `float()` in mutation transport with Decimal conversions at SDK boundary only.  
5. Upgrade/test `py-clob-client-v2` to latest 1.x or begin adapter-shaped `polymarket-client` execution spike **without** removing arming.  
6. User-stream + open-order reconciliation smoke on operator host.  
7. Two consecutive 5m read-only rollovers with READY books evidence.

### NEXT — resilience / maintainability

8. Optional Option B: REST bootstrap via `PublicClient.get_order_book` inside adapter.  
9. Metrics: rest_failures on fetch exception; WS received vs applied counters.  
10. Bounded optional raw WS capture for forensics (default off).

### LATER — optional modernization

11. Migrate authenticated paths from v2 → `polymarket-client` behind adapter.  
12. Evaluate SDK market stream only if BindingFeed maintenance cost justifies it.

### NO CHANGE

- Strategy math, BookView contracts, OMS architecture, risk budgets, discovery UP/DOWN rules.

---

## 11. Acceptance criteria (post-fix)

1. Header-less request still expected to 403; Tyrex UA request 200 on current tokens.  
2. Active+prepared feeds leave DESYNCED after successful snapshot path.  
3. `clob_book_events > 0` and snapshots_applied > 0 when venue emits books.  
4. Analytics show non-null ask on at least one ready leg when venue ask exists.  
5. `BOOK_FEE_READY` evaluated when model gates pass.  
6. No mutations in read-only proof runs.  
7. Two rollovers with persisted bindings and warm promote when venue permits.

---

## 12. Direct answers

1. **Was the last task conceptually correct?** Yes.  
2. **Did it introduce a regression?** It introduced a fragile REST client and overly strict recovery; it exposed Cloudflare 1010 rather than creating geoblock.  
3. **Is missing User-Agent the complete root cause?** It is the complete cause of REST 403. It is **not** the complete cause of zero applied WS events — DESYNCED drop policy is a second blocker.  
4. **Why zero WS events applied?** Received-or-receivable frames are not published while not READY; DESYNCED drops them; compose only counts dispatcher publishes.  
5. **Should the immediate header patch be made?** Yes.  
6. **Temporary or permanent?** Permanent **honest Tyrex UA** (and retries). Not a temporary Chrome spoof.  
7. **Should REST migrate to polymarket-client?** Not required for NOW; optional NEXT inside adapter.  
8. **Should market WS migrate too?** Not now. Custom BindingFeed remains justified.  
9. **Which custom integration remains justified?** Sync barrier, store, BookView, discovery semantics, OMS arming, normalize→contracts.  
10. **Which deps are deprecated?** `py-clob-client` v1 archived; `py-clob-client-v2` transitional; unified `polymarket-client` is current recommendation for new work.  
11. **Auth/wallet assumptions compliant?** Structurally aligned with CLOB V2 L1/L2 model; unified-SDK migration details unverified for Tyrex envelopes.  
12. **Tiny-live order semantics still protected?** Arming/FAK/caps exist, but `float()` and SDK version drift need attention before LIVE.  
13. **What else will break later if unchanged?** WAF/header fragility; DESYNCED dark feeds; SDK drift; fee schedule mismatch; eventual v2→unified migration pressure.  
14. **Smallest clean solution?** Honest UA REST + separate WS-snapshot recovery.  
15. **Exact sequence after approval?** (1) REST UA/retry patch + tests/probe, (2) DESYNCED recovery PR + tests, (3) short OBSERVE proof with non-null books, (4) BEFORE-LIVE checklist items, (5) only then operator tiny-LIVE review.

---

## 13. Evidence index

**Files:** `rest_book.py`, `book_feed.py`, `book_store.py`, `book_view.py`, `live_zgap_compose.py`, `n4_observe_runtime.py`, `ws_adapter.py`, `normalize.py`, `discovery.py`, `mutation_transport.py`, `sdk_readonly.py`, `pyproject.toml`, run dirs above, `implementation_report.md`.

**Probes (2026-07-30):**

- urllib no UA → 403 CF 1010  
- urllib Tyrex UA → 200, best bid/ask present  
- `py_clob_client_v2.get_order_book` → 200  
- `polymarket.PublicClient.get_order_book` → 200  
- market WS 8s → `book` + `price_change` parsed by Tyrex normalizer  

**Packages:** `polymarket-client==0.2.0` (probe install), `py-clob-client-v2==1.0.1` installed / `1.1.0` latest on PyPI.
