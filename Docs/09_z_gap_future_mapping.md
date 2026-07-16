# 09 — Z-Gap future mapping

**Phase:** Planning stub until R8 acceptance. **Do not implement Z-Gap in R1–R7.**

## Sequencing

After framework acceptance (R8):

1. **Z1** — Chainlink/RTDS, PTB capture/attestation, TimeAuthority, clock uncertainty, window boundary, calibration evidence.  
2. **Z2** — Observe: σ, fair value, fee-aware edge, entry/exit eval, facts; compare to historical validated runs under `old/` / `var/`.  
3. **Z3** — Shadow intents through shared risk/execution/portfolio.  
4. **Z4** — Tiny-live only with explicit authorization.

## Reference locations (historical)

Useful logic to port selectively (not import live):

- `old/src/tyrex_pm/quant/`
- `old/src/tyrex_pm/strategies/z_gap/`
- `old/src/tyrex_pm/ingestion/price_to_beat_tracker.py`
- `old/src/tyrex_pm/runtime/time_authority.py`
- Phase 0 notes: `old/Docs/Implementation/z_gap_architecture_reset/`

## Rule

Z-Gap-specific concepts must not enter generic modules during R2–R8.
