# PTB Commissioning Report

**Status:** pending — run `--commission-ptb` to populate certificate  
**Policy:** `z_gap_ptb_commissioning_v1`

## Independent reference source

| Field | Value |
|-------|-------|
| **Source** | `chainlink_aggregator_v3_eth_mainnet_btc_usd` |
| **Endpoint** | Ethereum JSON-RPC `eth_call` → Chainlink BTC/USD aggregator V3 `latestRoundData()` |
| **Contract** | `0xF4030086522a5bEEa6518e28D4d8B4E0E4688E3` (Ethereum mainnet) |
| **Field name** | `answer` (int256, 8 decimals); round timestamp from `updatedAt` |
| **Timestamp semantics** | `updatedAt` is the Chainlink round update time (unix seconds), compared to `event_start_ts` |
| **Availability delay** | On-chain round heartbeat ~3600s; commissioning accepts nearest round within 7200s of boundary |
| **Failure behavior** | Window excluded; certificate `status=invalid` if insufficient usable windows |
| **Why independent** | Direct Ethereum mainnet aggregator read — not Polymarket RTDS, not Tyrex sidecar, not Tyrex boundary K selection |

**Required env:** `TYREX_ETH_RPC_URL` or `ETH_RPC_URL`

**Not used (explicitly rejected as independent):** Polymarket Gamma `priceToBeat` (field absent), Polymarket RTDS relay, Tyrex sidecar log replay, live/log cross-check alone.

## Window evidence

Populated by:

```bash
python scripts/go_z_gap_tiny_live.py \
  --commission-ptb \
  --windows 3 \
  --run-name "z_gap_ptb_commissioning"
```

## Certificate

Path: `var/reporting/z_gap/ptb_commissioning_certificate.json`

Valid only when `status=valid`, `windows_usable >= windows_required`, all errors ≤ 0.5 bps, `source_mismatch_count=0`.
