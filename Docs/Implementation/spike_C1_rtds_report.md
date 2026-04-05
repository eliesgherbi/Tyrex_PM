# spike_C1_rtds_report — RTDS `activity` / `trades` (Phase 0.5)

**Status:** populated — live spike executed in validation run (2026-04-04).

## Environment

- **Date:** 2026-04-04  
- **Operator:** automated validation (Tyrex_PM workspace, network-enabled)  
- **Host:** `wss://ws-live-data.polymarket.com` (default)  
- **Command:** `python scripts/spike_rtds_activity.py --duration 15`  
- **Guru wallet filter:** not applied for bulk rate sample (`--wallet` omitted)

## Connection

- [x] Connected to `guru_rtds_url` — WebSocket opened; `on_open` fired.  
- **Failure notes:** none in this run.

## Subscribe envelope

Exact JSON sent after connect:

```json
{"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades"}]}
```

(Unfiltered — no `filters` key.)

## Message arrival

- [x] Non-empty messages received — continuous stream of JSON objects with `payload`.  
- **Framing:** spike only prints parsed lines where top-level object contains `"payload"` (aligned with Polymarket `real-time-data-client`).

## Message rate

- **Duration sampled:** 15.0 s  
- **Messages (payload-bearing):** 1242  
- **Approx. rate:** **~83 messages/s** (unfiltered global `activity`/`trades`)  
- **Implication:** v1 unfiltered path is **CPU/queue/drain sensitive**; acceptable only if host and `guru_stream_queue_drain_interval_ms` keep up; ops must confirm.

## Payload fields vs `GuruTradeSignal`

Observed **payload** keys (representative):  
`asset`, `bio`, `conditionId`, `eventSlug`, `icon`, `name`, `outcome`, `outcomeIndex`, `price`, `profileImage`, **`proxyWallet`**, `pseudonym`, `side`, `size`, `slug`, `timestamp`, `title`, **`transactionHash`**

**Mapping:** `tyrex_pm.data.guru_rtds_parse.rtds_payload_to_activity_row` normalizes to Data API–like rows; `trade_row_to_signal` / `ingest_source_trade_id` apply. **`proxyWallet`** is present and suitable for client-side guru filter.

## `proxyWallet` vs guru address

- [x] Field present on payloads (checksummed `0x…` strings).  
- **Normalized match:** operators must set **`guru_wallet_address` == proxy wallet** seen on stream (case-insensitive compare in code). **Not validated** against a specific deployed guru in this run (no `--wallet` filter against a production guru).

**Operator checklist:** run:

`python scripts/spike_rtds_activity.py --wallet <guru_proxy_from_polymarket> --duration 60`

and confirm non-zero `matched` during guru activity.

## Filtered vs unfiltered

- **Unfiltered subscription:** **works** (this run).  
- **With `filters`:** **not executed** in this validation (PowerShell quoting issue during quick attempt). **Manual:**  
  `python scripts/spike_rtds_activity.py --duration 30 --filtered-json "{\"event_slug\":\"<slug>\"}"`  
  Document whether server ignores or errors on filters.

## Burst / replay / reconnect

- **Initial burst:** not separately characterized in 15 s window; stream began immediately after subscribe.  
- **Reconnect:** not stress-tested in this run (single continuous session).

## Stall / liveness

- **Observed stall with socket open:** not in this short run.  
- **Production:** rely on `GuruStreamActor` / `RtdsActivityTradesWorker` liveness timeout + `STALL` / `RECONNECT` handling — **needs hours-long soak**.

## Duplicate `transactionHash` across different `asset`

In the same 15 s sample, **the same `transactionHash` appeared with different `asset` values** (multiple trades / legs per tx). Tyrex C1 **dedup uses `transactionHash:asset`** when `transactionHash` is present (`ingest_source_trade_id`) so legs are not collapsed.

**Risk:** a **second** guru trade leg sharing the same tx hash could be **incorrectly suppressed** as duplicate. **Gate / engineering:** confirm venue semantics or extend id (e.g. `tx:asset`) if multi-asset same-tx is possible for a single guru follow policy.

## Go / no-go (unfiltered v1)

**Recommendation:** **Conditional GO for shadow + restricted primary canary**, **not** blanket GO for broad primary until:

1. Ops accept **~80+ msg/s** sustained processing and monitoring.  
2. **Dedup / multi-leg tx** question is resolved or accepted as known limitation.  
3. **Shadow** run proves poll vs `guru_stream_would_emit` coverage for the target guru.  
4. **Soak** validates stall/recovery.

**One-line rationale:** RTDS `activity`/`trades` **works**, fields match Tyrex parser expectations, and `proxyWallet` is present; **unfiltered rate is non-trivial**; dedup is **`tx:asset`** when tx is present (multi-leg safe).

## Sign-off

- Automated validation run completed; production operator sign-off still required for guru-specific wallet match and long-run soak.
