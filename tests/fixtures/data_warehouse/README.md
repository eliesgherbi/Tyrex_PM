# Provider regression fixtures

`manifest.json` pins `protocol_examples.json` by SHA-256 and lists every case and its provenance. All current cases are **synthetic**. The linked provider documents explain the intended protocol families; these files are not evidence that a live provider emitted a particular shape.

Fixtures cover Gamma metadata/keyset envelopes, both outcome books, multi-token updates, trades, quotes, tick changes, CLOB time, reference history/live decimals, Binance trades/bootstrap/deltas and fractional resolution. Decoder tests validate the manifest and expected typed outputs. End-to-end tests additionally cover a windowless market and a non-price external note.

When operator capture becomes available, add small sanitized live examples with explicit observation dates, transport/endpoints, source units and a new provenance manifest. Never add authentication exchanges, `.env` content or private account data. Unknown threshold, payout, chain, timestamp or fee facts must remain unqualified until supported by evidence.
