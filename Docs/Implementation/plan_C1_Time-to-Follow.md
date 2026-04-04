# plan_C1_Time-to-Follow — Event-driven guru ingestion (C1)

Implementation planning document for **Phase C / C1 — Time-to-Follow**, aligned with `Docs/Implementation/phase_c_merged_plan.md` **objective** (maximize follower alpha capture) but **updating the C1 delivery target**: the minimum valuable C1 implementation is **event-driven guru ingestion**, not adaptive polling as the primary path.

---

## 1. Objective

**C1** reduces **time-to-follow** by making guru trade **detection** effectively real-time: replace **poll-interval-bound** discovery (`GET /activity`) with an **event-driven** ingestion path so follower detection latency is no longer dominated by `guru_poll_interval_seconds`. Downstream **signal → sizing → risk → submit** should stay intact; only the **source** and its **translation to `GuruTradeSignal`** change.

---

## 2. Why C1 changes now

`phase_c_merged_plan.md` still lists adaptive polling under C1 MVP; the team is **reprioritizing** so C1’s first shipped architecture is **websocket/stream-first**. Polling remains acceptable only as **fallback / rollout safety**, not as the design center—because poll interval is an upper bound on detection delay and directly drives **alpha leak #1** (detection latency) and worsens **guru price impact** (follower arrives later).

---

## 3. Current workflow audit (codebase-specific)

### 3.1 Discovery start and composition root

- **Entry:** `scripts/run_guru.py` → `tyrex_pm.runtime.guru_compose.build_guru_trading_node`.
- **Wiring:** `build_guru_trading_node` constructs `GuruMonitorActorConfig` from `RuntimeSettings`, instantiates `GuruMonitorActor`, `CopyStrategy`, registers with `node.trader.add_actor(guru)` then `add_strategy(strat)` (actor before strategy).

### 3.2 Polling actor and HTTP client

| Piece | Location |
|-------|----------|
| Actor | `src/tyrex_pm/data/guru_monitor.py` — `GuruMonitorActor` (`nautilus_trader.common.actor.Actor`) |
| HTTP | `src/tyrex_pm/data/data_api_client.py` — `PolymarketDataApiClient.get_user_trade_activity` → `GET {data_api_base_url}/activity` with `type=TRADE`, pagination (`activity_limit`, `max_activity_pages_per_poll`) |
| Incremental state | `src/tyrex_pm/data/guru_watermark.py` — `GuruWatermarkStore` (`last_seen_ts_ms` JSON); `src/tyrex_pm/data/guru_dedup.py` — `GuruDedupStore` (LRU + optional JSON persistence) |

### 3.3 Poll timer configuration

- **Code:** `GuruMonitorActor.on_start` calls `self.clock.set_timer(name="guru_monitor_poll", interval=timedelta(seconds=self._cfg.poll_interval_secs), callback=self._handle_poll_tick)`.
- **Config model:** `RuntimeSettings.guru_poll_interval_seconds` in `src/tyrex_pm/config/loaders.py`.
- **YAML:** e.g. `config/runtime/live_polymarket.yaml` → `guru_poll_interval_seconds: 30.0`, plus `guru_activity_limit`, `guru_max_activity_pages_per_poll`, `guru_startup_backfill_seconds`, `data_api_base_url`, paths `guru_state_path` / `guru_dedup_state_path`.

### 3.4 Internal signals and message bus

- **Topic:** `GURU_TRADE_TOPIC = "tyrex_pm.guru.GuruTradeSignal"` in `guru_monitor.py`.
- **Publish:** `GuruMonitorActor._publish_signal` → `self.msgbus.publish(GURU_TRADE_TOPIC, sig)`.
- **Consume:** `CopyStrategy.on_start` → `self.msgbus.subscribe(topic=GURU_TRADE_TOPIC, handler=self._on_guru_trade)` in `src/tyrex_pm/strategy/copy_strategy.py`.
- **Payload type:** `src/tyrex_pm/core/types.py` — `GuruTradeSignal` (`source_trade_id`, `ts_event_ms`, `side`, `token_id`, `size_raw`, `price_raw`, `raw_payload_ref`).

### 3.5 Parse path from API rows to signals

- `activity_trade_row_to_signal` / `trade_row_to_signal` / `stable_source_trade_id` in `src/tyrex_pm/data/guru_parse.py` (Data API `/activity` row shape).

### 3.6 Timestamps and latency loss today

| Timestamp / event | Role |
|-------------------|------|
| `GuruTradeSignal.ts_event_ms` | Venue-related time from API `timestamp` (normalized to ms in `guru_parse.api_timestamp_to_ms`). |
| Poll phase | `guru_poll_tick` logs `phase=on_start` / `phase=timer` / `sub=fetch` — **no** `ts_received_http` or detection latency metric. |
| **Dominant gap** | Worst-case delay ≈ **poll interval** + HTTP round-trip + pagination batching; backoff on errors uses `time.sleep` in-thread (`_error_backoff_sleep`). |
| Downstream | `CopyStrategy` logs `shadow_order_intent` / `live_order_intent` with `correlation_id` but **no** structured `ts_submit_ms` vs `ts_event_ms`. |

### 3.7 Websockets / streaming in this repo

- **Tyrex:** **No** websocket client for guru or Data API; only HTTP (`httpx`) in `PolymarketDataApiClient`.
- **Nautilus Polymarket (dependency):** `nautilus_trader[polymarket]>=1.220.0` registers live **CLOB** websocket clients when `polymarket_nautilus_live` and live mode (`guru_compose.py`):
  - **Data client** (`…/adapters/polymarket/data.py`): **MARKET** channel → book / quotes / **`last_trade_price`** (`PolymarketTrade` in `schemas/book.py`) → `TradeTick` etc. **Fields do not include maker/taker wallet or tx hash** — **cannot** assert “this trade is the guru’s.”
  - **Exec client** (`…/adapters/polymarket/execution.py`): **USER** channel (authenticated **follower** L2 creds) → `PolymarketUserOrder` / `PolymarketUserTrade` — **wrong identity** for copying **another** wallet.
- **`py-clob-client`:** `get_market_trades_events(condition_id)` is HTTP to `/live-activity/events/` — **not** wired into Tyrex guru flow.
- **Conclusion (verified):** Today’s **in-repo** Nautilus Polymarket websocket surfaces **do not** provide a third-party guru wallet activity feed. Event-driven guru ingestion requires a **new** integration (see §4).

### 3.8 Related flows (not guru detection)

- `src/tyrex_pm/runtime/guru_cache_warmup.py` — one-shot Data API `/activity` to warm `Cache` for dynamic instruments (resolution only).

### 3.9 Bottlenecks and assumptions

- **Assumption:** `guru_wallet_address` in strategy YAML matches the address that appears on trades in the **chosen** stream (Polymarket often uses **proxy** wallets — **must** match RTDS / API field used for filtering; see §10).
- **Bottleneck:** timer-driven polling; error backoff blocks the actor thread.
- **Strategy thickness:** `CopyStrategy` stays thin (subscribe + policies + risk + port); **good** — preserve this.

---

## 4. Event-driven target architecture

### 4.1 Source of truth for “guru traded”

- **Target:** Streaming **activity trades** with **wallet identity** in the payload, filtered to the configured guru address.
- **Recommendation (external, codified in Polymarket RTDS docs / `real-time-data-client`):** **RTDS** websocket — topic `activity`, type `trades` — payload includes `proxyWallet`, `transactionHash`, `asset`, `side`, `price`, `timestamp`, `slug` / `eventSlug`, etc. Subscribe with **`event_slug` / `market_slug` filters** per upstream docs; **client-side filter** `proxyWallet.lower() == guru_wallet_address.lower()` (unless docs add a user-address subscription filter).
- **Explicit non-option:** Reusing only Nautilus **CLOB MARKET** `last_trade_price` as guru detection — **rejected** (no wallet identity in `PolymarketTrade`).

### 4.2 Ownership

| Concern | Owner (target) |
|---------|----------------|
| WebSocket connect / reconnect loop | New **Tyrex** component (Actor or dedicated client owned by Actor) — *not* `CopyStrategy`. |
| Subscription set (markets/slugs/tokens) | Same Actor or small **`GuruRtdsSubscriptionController`** helper under `tyrex_pm/data/` or `tyrex_pm/runtime/`. |
| Raw message → `GuruTradeSignal` | `tyrex_pm/data/` parser module (parallel to `guru_parse.py`). |
| Dedup / idempotency | Reuse `GuruDedupStore`; optional watermark for REST **recovery** only. |
| Publish to followers | **Unchanged:** `msgbus.publish(GURU_TRADE_TOPIC, GuruTradeSignal)`. |

### 4.3 Downstream consumers

- **`CopyStrategy`**, **`ConfiguredRiskPolicy`**, **`NautilusGuruExecutionPort`** / `PolymarketExecutionPolicy` — **no contract change** if `GuruTradeSignal` stays stable.

### 4.4 Text diagram

```
                    ┌──────────────────────────────────────┐
                    │ RTDS WebSocket (activity / trades)    │
                    │ [new Tyrex client]                    │
                    └─────────────────┬────────────────────┘
                                      │ bytes / JSON
                    ┌─────────────────▼────────────────────┐
                    │ GuruIngestActor (or refactored monitor) │
                    │ - subscribe / reconnect                 │
                    │ - filter by guru wallet (+ optional     │
                    │   market/event filters)                 │
                    │ - parse → GuruTradeSignal              │
                    │ - dedup (GuruDedupStore)                │
                    └─────────────────┬────────────────────┘
                                      │ msgbus.publish
                                      │ topic: tyrex_pm.guru.GuruTradeSignal
                    ┌─────────────────▼────────────────────┐
                    │ CopyStrategy → entry/exit → sizing     │
                    │ → RiskPolicy → ExecutionPort            │
                    └────────────────────────────────────────┘

Parallel (fallback only): GuruMonitorActor poll → same topic (must not double-fire; dedup by tx-based id).
```

---

## 5. Recommended minimum implementation (v1)

### 5.1 In scope

1. **`GuruRtdsClient` (or equivalent)** — connect, subscribe, decode, callback; wire format mirrored from Polymarket’s `real-time-data-client` (subscribe JSON, message envelope).
2. **`GuruStreamActor`** (cleanest) **or** refactor **`GuruMonitorActor`** behind a `GuruIngestMode` enum (`stream` | `poll`) — publish **only** through shared `_ingest_signal(sig: GuruTradeSignal)` path.
3. **Parser** `rtds_activity_trade_to_signal(mapping) -> GuruTradeSignal` — align `source_trade_id` strategy with `stable_source_trade_id` (prefer `transactionHash` when present).
4. **Subscription v1 (minimal):**
   - Config list: e.g. `guru_rtds_event_slugs:` or derive from recent Data API `/activity` once at startup (same as warmup tokens → resolve slugs via Gamma if needed), **or** single broad filter if ops constraint allows — **trade-off** in §10.
5. **Instrumentation (mandatory)** — see §8.
6. **Reconnect** — resubscribe; on gap suspicion, **one-shot** REST `/activity` from `GuruWatermarkStore.last_seen_ts_ms` to fill holes (reuse existing client).

### 5.2 Out of scope (v1)

- C2 sizing / conviction, C3 execution quality beyond existing stack.
- Sub-10ms co-location optimizations.
- Feeding guru events through Nautilus `DataEngine` as `TradeTick` (unnecessary if msgbus stays the contract).

### 5.3 Fallback only

- **Polling `GuruMonitorActor`** behind config flag: e.g. `guru_ingest_primary: rtds | poll`; or `rtds_shadow_mode: true` for Phase 2 comparisons.

### 5.4 Day-one instrumentation

- `ts_ws_message_received_ns` (or ms), `ts_signal_published_ms`, correlation id, slug/token; counters: `rtds_reconnects`, `rtds_parse_errors`, `rtds_dedup_skips`, `poll_fallback_activations`.

---

## 6. Concrete code changes

### 6.1 Likely new files

- `src/tyrex_pm/data/guru_rtds_client.py` — websocket session + subscribe API (or `tyrex_pm/data/rtds/` package if split).
- `src/tyrex_pm/data/guru_rtds_parse.py` — RTDS trade payload → `GuruTradeSignal`.
- Optionally `src/tyrex_pm/data/guru_ingest_actor.py` — if splitting from `guru_monitor.py`.

### 6.2 Likely modified files

- `src/tyrex_pm/data/guru_monitor.py` — extract shared dedup+publish; or deprecate primary poll timer when stream mode.
- `src/tyrex_pm/runtime/guru_compose.py` — construct stream actor vs monitor; pass config.
- `src/tyrex_pm/config/loaders.py` — `RuntimeSettings` fields: ingest mode, RTDS URL override, subscription lists, reconnect timeouts.
- `config/runtime/*.yaml` — document new keys; keep poll keys for fallback.
- `tests/integration/` — mock websocket or recorded frames for parser + dedup.

### 6.3 Interfaces / protocols

- **`GuruIngestPort` (optional Protocol)** — `start()`, `stop()`, internal publish only; allows swapping RTDS vs future source without touching `CopyStrategy`.
- **Stable output:** unchanged **`GuruTradeSignal`** + **`GURU_TRADE_TOPIC`**.

### 6.4 Composition / wiring

- **`build_guru_trading_node`** — branch on `RuntimeSettings`: register **`GuruStreamActor`** instead of or alongside **`GuruMonitorActor`** (if alongside, **only one** primary publisher to msgbus unless shadow compare).

### 6.5 Config add / deprecate

| Add (proposal) | Purpose |
|----------------|---------|
| `guru_ingest_mode` | `rtds` \| `poll` |
| `guru_rtds_ws_url` | default Polymarket RTDS base if not hardcoded |
| `guru_rtds_subscriptions` | list of `{topic,type,filters}` |
| `guru_rtds_reconnect_backoff_*` | caps |
| `guru_poll_fallback_enabled` | allow poll when RTDS unhealthy |

| Deprecate (soft) | When RTDS primary stable |
|-------------------|--------------------------|
| tight coupling to poll interval as **primary** latency knob | keep `guru_poll_interval_seconds` for fallback |

---

## 7. Operational concerns (minimal)

- **Reconnect:** exponential backoff + cap; full resubscribe; increment counter.
- **Heartbeat / liveness:** if RTDS idle, rely on TCP/WS ping or application-level timeout + reconnect (confirm with Polymarket client behavior).
- **Backfill / recovery:** after reconnect or detect sequence gap, run **incremental** `GET /activity` from watermark → merge with **dedup**; advance watermark.
- **Duplicates:** `GuruDedupStore` + stable id from `transactionHash` (or composite if missing).
- **Ordering:** assume **partial order** per market; process idempotently; watermark is “max seen ts” not strict ordering proof.
- **Idempotency:** same as today — dedup before publish.
- **Fallback:** if RTDS fails N times or no messages for T seconds with known-active guru, **temporarily** enable poll tick (log `fallback_activation`).
- **Startup:** establish RTDS, subscribe, then **optional** REST catch-up from watermark; **shutdown:** cancel tasks, close WS cleanly.

---

## 8. Observability and validation

### 8.1 Metrics / logs (minimum)

| Metric / field | Meaning |
|----------------|---------|
| `guru_ws_message_ts` | Venue/payload `timestamp` from stream |
| `guru_ws_received_ts` | Local time message received |
| `guru_signal_emitted_ts` | Existing `guru_signal_emitted` extended |
| `follower_submit_ts` | On `live_order_intent` / `NautilusGuruExecutionPort.submit_intent` entry |
| `latency_detection_to_submit_ms` | `submit_ts - ws_received_ts` (and vs `ts_event_ms`) |
| `rtds_reconnect_total` | Counter |
| `rtds_subscription_errors_total` | Counter |
| `ingest_fallback_poll_active` | Gauge or periodic log |

### 8.2 Acceptance criteria (C1)

- **Median** `guru_ws_received_ts - ts_event_ms` (ingest lag) stable and **<<** previous median `poll_interval/2` under guru-active windows.
- **Median** `follower_submit_ts - guru_ws_received_ts` not regressing vs polling baseline (strategy/risk unchanged).
- **No** duplicate live submits for same `transactionHash` (dedup).
- **Shadow period:** side-by-side correlation — RTDS detects **≥** poll for same trades (allowing API indexing delay).

---

## 9. Rollout plan (tailored to repo)

| Phase | Action |
|-------|--------|
| **0** | **Instrumentation only** on current path: log `ts_event_ms`, add submit-side timestamp; baseline latency histogram from logs. |
| **1** | Implement RTDS client + parser **without** trading impact: `guru_ingest_mode=poll` + **shadow** RTDS listener logs “would emit” vs poll (`correlation_id` match rate). |
| **2** | **RTDS primary** + `guru_poll_fallback_enabled=true` (slow poll or on health failure); dedup shared. |
| **3** | Reduce poll to **recovery-only** (post-reconnect, explicit gap-fill) or disable in production after soak. |
| **4** | Remove dead poll timer from default configs (keep code path for emergencies). |

---

## 10. Open questions / blockers

1. **Wire protocol:** Exact RTDS URL, subscribe envelope, and error codes — **confirm** against current Polymarket docs / TypeScript reference implementation (version drift risk).
2. **`proxyWallet` vs `guru_wallet_address`:** Confirm strategy config address matches RTDS `proxyWallet` (case, proxy vs EOA); mismatch = silent no-fires.
3. **Subscription breadth:** `activity/trades` filters appear **market/event-centric** in public README — multi-market gurus may need **dynamic subscription updates** or **wider filters** (volume / noise trade-off).
4. **Legal / rate / fair-use:** RTDS connection limits and allowed fan-out — ops confirmation.
5. **Initial dump:** Whether RTDS sends historical burst on subscribe — must **dedup** and possibly **not** treat as “new alpha” for latency metrics.
6. **Dependency choice:** Implement WS with stdlib + `asyncio`, `websockets` lib, or Nautilus `WebSocketClient` — evaluate **message loop integration** with Nautilus `Actor` thread model (may need bridge: asyncio thread + callback to msgbus).

---

## Recommendation

| Question | Answer |
|----------|--------|
| **Is full event-driven guru ingestion feasible in the current architecture?** | **Yes**, without moving logic into `CopyStrategy`: keep **`GuruTradeSignal` + `GURU_TRADE_TOPIC`**. **No** to “only reuse existing Nautilus CLOB websockets for guru detection” — **in-repo** MARKET and USER feeds **cannot** attribute trades to the guru wallet. **Feasible path:** add **RTDS (or equivalent Polymarket-documented activity stream)** client in Tyrex + thin Actor. |
| **Recommended path?** | New **`GuruStreamActor`** (or refactored monitor) using **RTDS `activity`/`trades`**, client-side **wallet filter**, shared **dedup** + **REST watermark backfill** on reconnect; **polling** demoted to **fallback / gap-fill**. |
| **Minimum viable event-driven implementation?** | RTDS subscribe + parse → `GuruTradeSignal` → existing msgbus; config-driven subscriptions v1; **mandatory** latency logs; **shadow** validation phase before primary. |
| **Should polling remain as fallback?** | **Yes**, until RTDS soak proves stability. **Role:** reconnect backfill + health watchdog path; **duration:** retire primary poll after metrics in §8.2 hold for agreed soak window (e.g. 1–2 weeks ops-defined). |

**Opinionated summary:** Replace the **source**, not the pipeline. Nautilus Polymarket remains essential for **execution and book state**; guru **intent detection** should **not** be forced through `PolymarketDataClient` trade ticks—those lack guru identity. The smallest honest MVP is **one new streaming integration + parser** and a **compose-time** switch.
