# M2B.2 — External BTC feed

## 1. Purpose in simple terms

Add a **read-only** Binance WebSocket data venue that emits `external_btc_tick` and `clock_sync` events on the same event backbone, recorded alongside Polymarket data. No credentials, no order path, no wallet — data-only by construction.

## 2. Boundary

### In scope

- `venue/binance_data/` — WS client, message normalization
- `ingestion/external_btc.py` — async ingest loop
- `external_btc_tick` event emission (enum stub from M2B.0-A)
- Record in `cmd_record` when enabled
- Isolation test: package cannot import OMS/wallet

### Out of scope

- Multi-venue BTC
- BTC-led trading logic (Phase 3)
- Alpha signals
- Changes to paired-binary decisions

## 3. Current repo reality

| Path | State |
|------|-------|
| `venue/polymarket/` | Only trading venue today |
| `EventType.EXTERNAL_BTC_TICK` | Enum stub from M2B.0-A |
| `websockets` | In `[live]` optional extra |
| M2B.1-B | Recorder accepts events from multiple ingest tasks |

## 4. Files to create

| File | Why |
|------|-----|
| `src/tyrex_pm/venue/binance_data/__init__.py` | Data-only venue boundary |
| `src/tyrex_pm/venue/binance_data/ws_client.py` | Binance WS |
| `src/tyrex_pm/venue/binance_data/normalize.py` | Tick normalization |
| `src/tyrex_pm/ingestion/external_btc.py` | Ingest supervisor |
| `tests/test_binance_data_isolation.py` | No OMS/wallet imports |
| `tests/test_external_btc_events.py` | Event shape + reconnect |

## 5. Files allowed to modify

| File | Modification |
|------|--------------|
| `runtime/record_run.py` | Start BTC ingest task when configured |
| `runtime/config.py` | `ExternalBtcConfig` |
| `ingestion/sequencer.py` | Cross-source ordering by `recv_ts` (if shared sequencer) |

## 6. Forbidden files / modules

```text
execution/*, risk/*, state/wallet_store.py, paired_binary_run.py
```

## 7. Interfaces and contracts

### Config

```yaml
runtime:
  external_btc:
    enabled: false
    streams: [bookTicker, aggTrade]  # per plan §9 rev2
```

### Event payload (sketch)

```python
payload = {
    "source": "binance_perp",
    "bid": str, "ask": str, "mid": str,
    "stream": "bookTicker",
}
```

### Isolation rule

`venue/binance_data/` may import: `core`, `httpx`/`websockets`. Must not import `execution`, `risk`, `state.wallet_store`.

## 8. Runtime flags and rollback

`external_btc.enabled: false` default. Rollback: disable flag.

## 9. Tests required

Isolation test + event normalization fixtures + reconnect behavior.

## 10. Regression tests required

M2B.1 record tests; PM ingest equivalence unchanged.

## 11. Acceptance criteria

- [ ] 24h joint PM+BTC record-only run
- [ ] `clock_sync` samples in manifest
- [ ] Isolation test passes
- [ ] No trading imports in binance_data tree

## 12. Divergence risks

| Risk | Control |
|------|---------|
| Trading API keys in binance package | Public streams only |
| BTC events driving live strategy | No consumer in live path |

## 13. Review checklist

- [ ] Isolation test in CI
- [ ] Events recorded identically to PM events
- [ ] Reconnect does not crash record loop

## 14. Done / not done examples

**Done:** `external_btc_tick` lines in JSONL alongside `book_delta`.  
**Not done:** PM–BTC lead-lag notebook (M2B.4).

## 15. Next milestone dependency

**M2B.3** normalizes `btc_ticks` table from these events.
