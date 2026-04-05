# plan_C1_Time-to-Follow — Event-driven guru ingestion (C1)

Parent planning document for **Phase C / C1 — Time-to-Follow**: **event-driven guru ingestion** as the design center, **stable** `GuruTradeSignal` + `GURU_TRADE_TOPIC`, **polling demoted** to shadow / fallback / recovery—not latency architecture.

---

## Roadmap alignment (what this plan does **not** change)

C1 here is a **source replacement + observability upgrade** for guru detection. It does **not**:

- alter **Phase A/B** ownership (framework-visible state, risk/runtime consuming real state),
- move bookkeeping into `CopyStrategy` or thicken strategy beyond today’s subscribe → policy → risk → port path,
- redesign **C2** (capital allocation) or **C3** (execution quality),
- replace **Nautilus Polymarket**’s role for **follower** execution and market/user feeds where already wired.

---

## 1. Objective

Reduce **time-to-follow** by removing **poll-interval-bound** guru discovery (`GET /activity` timer) as the primary path. **Detection** should be **event-driven** so latency is not dominated by `guru_poll_interval_seconds`. Downstream **signal → sizing → risk → submit** stays unchanged; only ingestion **source** and **translation into `GuruTradeSignal`** evolve.

---

## 2. Why C1 changes now

Polling sets a hard upper bound on **detection delay** (alpha leak #1) and worsens **guru price impact**. C1 implementation target is **stream-first**; polling remains **operational safety**, not the design center.

---

## 3. Evidence classification

Do not treat the bullets below as interchangeable.

### 3.1 Verified in **current Tyrex codebase**

- Guru discovery is **`GuruMonitorActor`** (`guru_monitor.py`): `clock.set_timer("guru_monitor_poll", …)` → **`PolymarketDataApiClient.get_user_trade_activity`** (`data_api_client.py`) → `activity_trade_row_to_signal` (`guru_parse.py`) → **`msgbus.publish(GURU_TRADE_TOPIC, sig)`**.
- **Composition:** `build_guru_trading_node` (`guru_compose.py`) registers **`GuruMonitorActor`** then **`CopyStrategy`**; strategy subscribes to **`GURU_TRADE_TOPIC`** (`copy_strategy.py`).
- **Types:** `GuruTradeSignal` (`core/types.py`); topic constant in `guru_monitor.py`.
- **State:** `GuruWatermarkStore`, `GuruDedupStore` (`guru_watermark.py`, `guru_dedup.py`); config via `RuntimeSettings` (`loaders.py`) / runtime YAML (`guru_poll_interval_seconds`, etc.).
- **Nautilus Polymarket (installed dependency):** MARKET WS **`last_trade_price`** (`PolymarketTrade`) has **no** wallet / tx fields suitable for guru attribution; USER WS is **follower-authenticated**. **Forcing guru detection through those feeds is incorrect** for copy-trading **another** wallet.

### 3.2 Externally sourced (docs, REST shape, community reports) — **not** proven in Tyrex

- Polymarket **REST** trade/activity-style payloads are described with fields such as **`proxyWallet`**, **`asset`**, **`side`**, **`size`**, **`price`**, **`timestamp`**, **`eventSlug`**, **`transactionHash`** (useful **reference** for parser design; exact RTDS field parity is **not** code-verified here).
- **Proxy wallet model:** visible / profile / funder address is often the **proxy**, not EOA — affects **`guru_wallet_address`** correctness (operational validation required).
- **RTDS documentation gaps:** official RTDS docs **may not** clearly document **`activity` / `trades`** at the time of planning — treat stream API as **integration-dependent**.
- **Community/issue signals (unverified in repo):** reports that **empty-filter** `activity/trades` works while **filtered** subscriptions (`market_slug`, `event_slug`) **do not**; reports of **liveness / stall** over time despite an **open** socket — **inputs to spike**, not facts for design certainty.

### 3.3 Still to confirm by **Phase 0.5 spike** (mandatory before implementation tickets)

- RTDS connect URL, subscribe envelope, message framing, and error behaviors.
- Whether **`activity/trades`** delivers messages at all under Tyrex’s runtime constraints.
- **Message rate** (unfiltered global trades) vs operational limits.
- Whether **`proxyWallet`** on real stream payloads matches **operational `guru_wallet_address`** (and normalization rules).
- Whether **filtered** subscriptions work; if not, v1 default (unfiltered + client filter) is **confirmed** or must be revised.
- **Replay / initial burst** on subscribe; ordering and duplicate semantics on reconnect.
- **Stall-with-open-socket** behavior and need for **hard reconnect** / **liveness timeout**.

### 3.4 Phase 0.5 spike deliverable (named artifact)

**Implementation estimates, rollout dates, and production confidence in unfiltered RTDS remain provisional until a written spike artifact exists.**

- **Artifact:** `Docs/Implementation/spike_C1_rtds_report.md` (filled in after running `scripts/spike_rtds_activity.py` and capturing observations).
- **The report must capture:** connection success/failure; **subscribe envelope** actually used; confirmation of **real message arrival**; observed **message rate**; **`proxyWallet`** validation vs a known deployed **`guru_wallet_address`**; **filtered vs unfiltered** subscription behavior; **burst / replay / reconnect** notes; **stall / liveness** notes; explicit **go / no-go** for unfiltered RTDS as v1 production source.

---

## 4. Current workflow audit (codebase-specific)

| Stage | Module / symbol |
|-------|------------------|
| Entry | `scripts/run_guru.py` → `build_guru_trading_node` |
| Poll actor | `GuruMonitorActor` — `on_start`: immediate `_poll_trades_resilient`, then timer `guru_monitor_poll` |
| HTTP | `PolymarketDataApiClient.get_user_trade_activity` → `GET …/activity`, `type=TRADE`, pagination |
| Incremental cursor | `GuruWatermarkStore` (JSON `last_seen_ts_ms`) |
| Dedup | `GuruDedupStore` (`source_trade_id`) |
| Publish | `GuruMonitorActor._publish_signal` → `msgbus.publish(GURU_TRADE_TOPIC, ...)` |
| Consume | `CopyStrategy.on_start` → `msgbus.subscribe(GURU_TRADE_TOPIC, _on_guru_trade)` |
| Config | `RuntimeSettings.guru_poll_interval_seconds`, `guru_activity_limit`, `guru_max_activity_pages_per_poll`, paths, `data_api_base_url` |
| Latency today | Dominated by **poll interval**; structured **receive-time vs submit-time** logging largely absent |
| Unrelated | `guru_cache_warmup.py` — **Cache** warm from Data API, not the bus signal path |

---

## 5. Target architecture (post–Phase 0.5)

### 5.1 Locked v1 actor shape (**validated recommendation**)

**v1 uses a new `GuruStreamActor`** (Nautilus `Actor`). **`GuruMonitorActor` stays intact** for fallback, shadow comparison, and recovery.

**Code-based reasoning:** `GuruMonitorActor` couples **timer lifecycle**, **httpx polling**, **watermark advancement**, and **msgbus publish**. Folding RTDS into it would mix **async/long-lived socket** concerns with **synchronous poll + `time.sleep` backoff** in one class, complicating testing and shadow/dual-path wiring. A **separate actor** keeps poll behavior **unchanged** for operators, allows **both** actors registered during shadow (only one publishes in primary mode), and limits risky edits to a **new** file. Shared logic = **small extracted helpers** (dedup + publish + optional “normalize to `GuruTradeSignal`” glue), not a rewrite of the poller.

### 5.2 Locked default v1 subscription (**validated recommendation**)

**Default v1:** RTDS topic **`activity`**, type **`trades`**, subscription **without** `market_slug` / `event_slug` filters (**empty filter** as required by spike if that is the only reliably working mode), then **client-side** keep only rows where **`proxyWallet`** matches **`guru_wallet_address`** (normalized compare per spike).

**Justification:** (1) **Wallet-attributed** detection requires a payload field like `proxyWallet` — MARKET `last_trade_price` cannot do this (§3.1). (2) v1 avoids **market-universe management** and **fragile server-side filters** (§3.2–3.3). (3) Reduces “missed guru entry into new markets” vs slug-filtered subs. **Cost:** higher **ingest volume** before client filter — acceptable for C1 if spike shows rate is operable; if not, **reopen subscription strategy** after data.

**RTDS filter support is untrusted** until Phase 0.5 proves otherwise; do not build v1 logic that **depends** on slug filters.

### 5.3 Diagram

```
  RTDS WS (spike-validated URL/protocol)
           │
           ▼
  GuruStreamActor  ──parse/dedup──►  msgbus.publish(GURU_TRADE_TOPIC, GuruTradeSignal)
           │                                    │
           │                                    └──► CopyStrategy → risk → execution
           │
  [Primary phase: sole publisher of guru signals from stream path]

  GuruMonitorActor (timer) ──► same topic ONLY when mode = shadow publish,
                               fallback, or recovery-only (see §9)
```

### 5.4 Explicit **non-goals**

Do **not** route guru detection through **Nautilus Polymarket MARKET or USER** websockets (§3.1). Do **not** change **`GuruTradeSignal`** schema or **`GURU_TRADE_TOPIC`** string for C1.

---

## 6. Operational concerns (v1 minimum — mandatory)

| Concern | Requirement |
|---------|-------------|
| **Stall with open socket** | Assume **no useful data** despite TCP/WS “connected”. Track **last message time** (per connection and globally for ingest health). |
| **Heartbeat / ping** | Use **framework/library-appropriate** WS ping if available; if not, rely on **application liveness** (timeouts below). **Exact mechanism = implementation choice** validated against stack during build. |
| **Liveness timeout** | If **no accepted messages** (or no **post-filter guru hits** when globally quiet — **tune per spike**) exceed **`T_live`**, treat as unhealthy: **force reconnect** or escalate. |
| **Reconnect policy** | Bounded exponential backoff; **full resubscribe**; increment **`rtds_reconnect_total`**; log reason (error, liveness, parse storm). |
| **Post-reconnect gap-fill** | **Mandatory:** run **incremental** `GET /activity` from **`GuruWatermarkStore.last_seen_ts_ms`** through existing **`PolymarketDataApiClient`**, merge with **`GuruDedupStore`**, advance watermark — same idempotency as today. |
| **Dupes / ordering** | **Dedup before publish**; ordering **best-effort**; watermark = **progress marker**, not strict total order proof. |
| **Fallback polling activation** | **Criteria (configurable thresholds):** e.g. **N** consecutive reconnect failures, or **liveness timeout** fired **M** times within window, or operator flag. Log **`ingest_fallback_poll_active=1`** with reason code. **Exit fallback** when stream health sustained for configured window (separate hysteresis to avoid flapping). |

**Threading / async:** Nautilus `Actor` vs asyncio `WebSocketClient` integration is an **implementation recommendation to validate** against `TradingNode` / actor runtime in-repo — **not** a fixed up-front design from this document. Spike may prototype bridge; tickets must record chosen pattern.

---

## 7. Rollout phases and fallback semantics

| Phase | Polling (`GuruMonitorActor`) | RTDS (`GuruStreamActor`) |
|-------|------------------------------|---------------------------|
| **0** | **Unchanged**; primary publisher | **Off** |
| **0.5 — Spike (prerequisite)** | Optional: unchanged production baseline | **Standalone script or branch build**: connect, subscribe, measure rate, validate payload + **`proxyWallet`**, filter behavior, burst/reconnect — **no** dependency for merging implementation plan until written spike report |
| **1 — Shadow** | **Publishes** `GuruTradeSignal` (real behavior) | **Subscribes only**: parse + **log** “would emit”; compare **`source_trade_id` / tx** to poll path — **no** `msgbus.publish` from stream |
| **2 — Primary** | **Disabled** except **health-failure activation** (§6) and **post-reconnect REST gap-fill** (does **not** need timer if gap-fill is on-demand after reconnect; if timer used, **slow** interval only when stream unhealthy) | **Sole publisher** of guru signals to `GURU_TRADE_TOPIC` |
| **3 — Steady state** | **Recovery-only or off** by default (gap-fill + optional rare health poll); ops policy decides whether to keep emergency timer disabled in YAML | Primary |

**At most one code path** may **publish** guru signals to the bus in primary mode (stream **or** poll, never both).

---

## 8. Observability and validation

### 8.1 Technical metrics (minimum)

`guru_ws_received_ts`, payload `timestamp` → `ts_event_ms`, `guru_signal_emitted_ts`, `follower_submit_ts`, `latency_detection_to_submit_ms`, reconnect/error counters, **`stall_detected`**, **`fallback_activation`** with reason, **`gap_fill_runs`** post-reconnect.

### 8.2 Business-facing metric (alpha capture linkage)

At least one:

- **Coverage equivalence (shadow):** % of poll-detected guru trades that RTDS **also** observed within **target latency** (e.g. ≤ **L** seconds from `ts_event` or from first poll visibility — define **L** in spike/review), **or**
- **Opportunity rate:** % of guru trades (by **`transactionHash`**) that become **internal** `GuruTradeSignal` **published** within **L** — trending **up** vs polling baseline while duplicates stay bounded.

C1 validation is **not** only transport latency; it must show **fewer missed or materially delayed follow opportunities** under equivalent downstream policy.

### 8.3 Acceptance (engineering + business)

- Ingest and end-to-end latency metrics improve vs Phase 0 baseline; **no** duplicate **live** submits (dedup).
- Shadow: **coverage** metric meets agreed floor before primary flip.
- **`guru_wallet_address`** validated against real **`proxyWallet`** (startup checklist / runbook note).

---

## 9. Concrete code changes (orientation)

### 9.1 Dedup / `source_trade_id` precedence (frozen for v1)

**`source_trade_id` = `f"{transactionHash}:{asset}"`** when `transactionHash` is a non-empty string (`asset` normalized to string, stripped; empty suffix if missing) so same-tx multi-leg trades on different outcome tokens do not dedupe as one. **Otherwise** a deterministic composite fallback (timestamp + asset + side + size + price, same family as the historical composite). Construction is centralized in **`tyrex_pm.data.guru_parse.ingest_source_trade_id`** (and parsers build `GuruTradeSignal` from that). **Shadow** “would emit” logs and **poll** path must use the **same** precedence so coverage comparisons match.

### 9.2 Proposed runtime config keys (C1 v1)

| Key | Intent |
|-----|--------|
| `guru_ingest_mode` | `poll_only` \| `rtds_shadow` \| `rtds_primary` — selects ingestion path behavior |
| `guru_ingest_phase` | Optional alias / rollout tag (`0`…`3`) for ops; may mirror `guru_ingest_mode` |
| `guru_rtds_url` | RTDS WebSocket URL (default `wss://ws-live-data.polymarket.com`) |
| `guru_rtds_liveness_timeout_seconds` | No **any** RTDS message before forced reconnect / fallback escalation |
| `guru_rtds_reconnect_backoff_initial_seconds` | First backoff after disconnect/error |
| `guru_rtds_reconnect_backoff_max_seconds` | Backoff cap |
| `guru_poll_fallback_enabled` | Allow poll path to activate when stream declares fallback |
| `guru_poll_fallback_interval_seconds` | Poll interval when fallback timer is active (may exceed legacy default) |
| `guru_gap_fill_enabled` | Run REST `/activity` gap-fill after reconnect |
| `guru_gap_fill_lookback_seconds` | Lower bound for REST window if watermark stale (TBD tuning) |
| `guru_proxy_wallet_validation_required` | If true, fail startup unless wallet format check passes (non-empty `0x` address) |

Exact defaults and validation rules live in `load_runtime_settings`; some values stay **TBD** until `spike_C1_rtds_report.md` is filled.

### 9.3 Go / no-go: unfiltered RTDS as production v1 source

Unfiltered **`activity/trades`** + client **`proxyWallet`** filter is **accepted** as v1 **only if** spike + shadow show:

| Criterion | Direction |
|-----------|-----------|
| **Message rate** | Sustainable for target host (e.g. sustained msgs/sec below ops-agreed ceiling; no systematic API disconnects). |
| **CPU / memory** | Overhead bounded; no chronic GC or thread buildup from ingest. |
| **Stability / liveness** | Reconnect + ping policy recovers from real stalls; false-positive liveness trips within tolerable rate. |
| **Coverage vs poll (shadow)** | RTDS sees guru trades at least as often as poll baseline for the same wallet (allowing tie-break rules in the spike report). |
| **Payload → `GuruTradeSignal`** | Required fields mappable without silent drops; **proxy** field matches configured guru address when normalized. |

If **any** criterion **fails** in spike or shadow, **do not** promote unfiltered RTDS to primary — **revisit subscription strategy** (filters, narrower topics, or alternate source) before rollout.

### 9.4 File / module list

| Action | Likely location |
|--------|-----------------|
| **New** | `GuruStreamActor`, RTDS client module(s), `*_rtds_parse.py` → `GuruTradeSignal` |
| **Shared helpers** | Optional `guru_ingest_common.py` (dedup + publish + watermark touch) extracted from `guru_monitor.py` **only** if duplication would otherwise harm review |
| **Unchanged contract** | `core/types.py` `GuruTradeSignal`; `GURU_TRADE_TOPIC`; `CopyStrategy` subscription |
| **Compose** | `guru_compose.py`: register stream actor; mode flags for shadow / primary / fallback |
| **Config** | `RuntimeSettings` + YAML |
| **Tests** | Parser unit tests; recorded WS fixtures post-spike |

---

## 10. Open questions (post–Phase 0.5)

Exact thresholds (`T_live`, `N`, `M`), global message rate acceptability, field-level parser mapping from RTDS JSON to `GuruTradeSignal`, and **legal/ops** limits on unfiltered **`activity/trades`** — **close in spike report + ops sign-off**, not in this doc.

---

## Validated recommendations (checklist)

| # | Recommendation | Status |
|---|----------------|--------|
| 1 | **New `GuruStreamActor` for v1**; keep **`GuruMonitorActor`** for fallback/shadow | **Adopted** (§5.1) |
| 2 | **Default v1 source:** `activity/trades` **unfiltered** + **client-side `proxyWallet` filter** | **Adopted** (§5.2) |
| 3 | **RTDS server-side filters untrusted** until spike-verified | **Adopted** (§3.3, §5.2) |
| 4 | **Polling only** shadow / fallback / recovery | **Adopted** (§7) |
| 5 | **`GuruTradeSignal` + `GURU_TRADE_TOPIC` unchanged** | **Adopted** (§5.4) |
| 6 | **Do not** force guru detection through Nautilus Polymarket **MARKET/USER** WS | **Adopted** (§3.1, §5.4) |
| 7 | **Startup validation** that **`guru_wallet_address`** matches **proxy-wallet field** seen on venue/stream | **Required** (§3.2, §8.3) |
| 8 | **Liveness + post-reconnect REST gap-fill** mandatory in v1 | **Adopted** (§6) |
| 9 | **Threading/async bridge** validated against **actual** Tyrex/Nautilus runtime in implementation | **Adopted** (§6 footer) |

---

## Recommendation (executive)

| Question | Answer |
|----------|--------|
| **Is full event-driven guru ingestion feasible?** | **Yes** at the **architecture** level: **`GuruTradeSignal` + msgbus** already decouple detection from policy. **Feasibility of RTDS as the concrete stream** is **spike-gated** (§3.3). |
| **Recommended v1 path?** | **`GuruStreamActor`**, RTDS **`activity/trades`** default **unfiltered** + **`proxyWallet`** match, **dedup + watermark-backed REST gap-fill**, **liveness + reconnect**; **`GuruMonitorActor`** for shadow / fallback / recovery per §7. |
| **What must be spike-validated first?** | **Phase 0.5**: connect, subscribe, **message rate**, **payload fields**, **`proxyWallet` ↔ guru config**, **filter vs empty-filter behavior**, **burst/reconnect**, **stall** observations — **prerequisite** to locking implementation estimates. |
| **Polling role during rollout and after soak?** | **Shadow:** poll publishes; stream logs compares. **Primary:** poll **off** unless **health/fallback** (§6) or **gap-fill** after reconnect. **Steady:** **recovery-only or disabled** by default per ops. |

If Phase 0.5 shows **unfiltered** ingest is **unusable** (rate, ToS, or reliability), **revisit subscription strategy** with **spike data** — not speculative optimization in C1 scope.
