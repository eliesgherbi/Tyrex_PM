# v1.01 market resolution notes (auto-generated skeleton)

**Nautilus BinaryOption / instrument provider:** https://nautilustrader.io/docs/nightly/integrations/polymarket/

Re-run `python scripts/resolve_markets.py --write-notes Docs/validation/v1_01_resolution_notes.md` after changing `config/v1_markets.yaml`.

**Instrument refresh:** `PolymarketDataClient` refreshes the catalogue on a configurable interval (~60 min in Nautilus Polymarket docs). Live nodes should tolerate tick metadata updates.

## gta-vi-released-before-june-2026

- **Allowlist note:** Reference market used in Nautilus Polymarket docs; verify still active before production.
- **instrument_id:** `0xcccb7e7613a087c132b69cbf3a02bece3fdcb824c1da54ae79acc8d4a562d902-8441400852834915183759801017793514978104486628517653995211751018945988243154.POLYMARKET`
- **token_id (loader / YES outcome default):** `8441400852834915183759801017793514978104486628517653995211751018945988243154`
- **price_increment (Nautilus):** `0.001`
- **CLOB tick_size:** `0.001`
- **neg_risk (Gamma metadata):** `False`
- **Book:** `ok_liquidity` — bids=20_asks=72
- **resolved_at_utc:** `2026-03-27T22:38:39.712441+00:00`
- **Active vs excluded (manual):** reviewer must confirm `see book_status / Gamma metadata at resolution time` in UI/API.
