# M2B.1-B — Full recorder phase gate

## 1. Purpose in simple terms

Scale record-only mode from one-market smoke to **production data-lake workhorse**: discover all active BTC 5m markets, record 24 hours with compression, heartbeat monitoring, and coverage reporting. This is the phase-level acceptance gate for the recorder program.

## 2. Boundary

### In scope

- `ingestion/market_discovery.py` — poll Gamma/CLOB for BTC 5m markets
- JSONL.zst compression via optional `zstandard` dependency
- Heartbeat + coverage alerting
- Optional in-trading tap flag: `runtime.recording.tap_in_live` (default **false**)
- 24h unattended record-only acceptance
- Update `Docs/OPERATIONS.md`

### Out of scope

- BTC feed (M2B.2)
- Normalizer (M2B.3)
- Replay engine (M2B.5)
- Changing trading decisions
- `paired_binary_run` refactor

## 3. Current repo reality

| Path | State |
|------|-------|
| M2B.1-A artifacts | Assumed: `EventSink`, `cmd_record`, plain JSONL |
| `venue/polymarket/gamma_client.py` | Exists — discovery can reuse |
| `runtime/paired_binary_metadata.py` | BTC 5m market ID regex `_BTC_MARKET_RE` |
| `pyproject.toml` | No `zstandard` yet |

## 4. Files to create

| File | Why |
|------|-----|
| `src/tyrex_pm/ingestion/market_discovery.py` | Low-cadence market polling + subscription lifecycle |
| `config/scenarios/record_btc5m.yaml` | Full multi-market record scenario |
| `tests/test_market_discovery.py` | Discovery fixture parsing |
| `tests/test_event_sink_zstd.py` | Round-trip compressed segments |

## 5. Files allowed to modify

| File | Modification |
|------|--------------|
| `reporting/event_sink.py` | zstd writer option |
| `runtime/record_run.py` | Discovery loop, heartbeat |
| `runtime/config.py` | Discovery + heartbeat + `tap_in_live` settings |
| `runtime/app.py` | Optional in-trading tap wiring in `cmd_run` when flag on |
| `pyproject.toml` | `[project.optional-dependencies] record = ["zstandard>=0.22"]` |
| `Docs/OPERATIONS.md` | 24h record ops |

## 6. Forbidden files / modules

```text
risk/engine.py, execution/oms.py — no trading path changes
research/* — not yet
```

## 7. Interfaces and contracts

### market_discovery.py

```python
async def run_market_discovery(
    *,
    stop: asyncio.Event,
    on_market_discovered: Callable[[MarketDiscoveryResult], Any],
    poll_interval_s: float = 25.0,
) -> None: ...
```

Emits `MARKET_DISCOVERED` / lifecycle events into shared recorder.

### Compression

- File extension: `.jsonl.zst`
- Plain JSONL remains readable via decompress utility in `research/lib` (M2B.3) or `zstd -d`

### Heartbeat

- Periodic manifest heartbeat file or fact-style sidecar log
- Alert when no events written for configurable window

### Config additions

```yaml
runtime:
  recording:
    tap_in_live: false
    discovery_poll_s: 25
    heartbeat_stall_s: 120
    compress: true
```

## 8. Runtime flags and rollback

| Flag | Default |
|------|---------|
| `recording.tap_in_live` | `false` |
| `recording.compress` | `true` in full scenario |

Rollback: use M2B.1-A single-market plain JSONL scenario.

## 9. Tests required

| Test | Proves |
|------|--------|
| `test_market_discovery.py` | Parses fixture Gamma response |
| `test_event_sink_zstd.py` | Write/read round-trip |
| `test_record_heartbeat.py` | Stall detection fires |

## 10. Regression tests required

M2B.1-A tests remain green. M2B.0-B equivalence tests green.

## 11. Acceptance criteria

- [ ] 24h record-only run completes
- [ ] ≥95% of BTC 5m markets in window have manifests
- [ ] Heartbeat alerts tested on synthetic stall
- [ ] `zstandard` optional extra documented in OPERATIONS
- [ ] `tap_in_live` default false; live behavior unchanged when false

## 12. Divergence risks

| Risk | Control |
|------|---------|
| Enabling tap_in_live by default | Default false |
| Blocking discovery on trading | Separate asyncio task |
| Making zstandard required dep | Optional extra only |

## 13. Review checklist

- [ ] Discovery does not import OMS/wallet
- [ ] Compression optional with plain JSONL fallback
- [ ] Coverage report artifact produced
- [ ] OPERATIONS.md updated

## 14. Done / not done examples

**Done:** 24h `var/recordings/` tree with zstd segments per market.  
**Not done:** Parquet tables (M2B.3).

## 15. Next milestone dependency

**M2B.2**, **M2B.3** depend on recorded archives from this milestone.

**Handoff:** Multi-market JSONL.zst corpus + manifests + coverage report.
