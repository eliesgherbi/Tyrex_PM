# N1 audit tools (read-only)

Public market-data only. No auth, wallets, orders, signers, or `old/` imports.

| Script | Purpose |
|--------|---------|
| `capture_sources.py` | RTDS Chainlink + RTDS crypto + Binance Spot + Gamma poll → JSONL |
| `poll_displayed_ptb.py` | SSR `openPrice` attestation (matched queryKey only) |
| `analyze_capture.py` / `finalize_n1.py` | Boundary rules, latency, causal basis |
| `validate_discovery.py` | Slug/Gamma/Up-Down mapping checks |
| `probe_open_price.py` | One-off PTB URL probes |

Outputs under `var/reporting/n1/` (gitignored). See
`Docs/implementation/z_gap_production_readiness/n1_acceptance_report.md`.
