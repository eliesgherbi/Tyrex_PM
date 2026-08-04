# N3B live PTB seal

Public/read-only Chainlink EXACT boundary capture + SSR displayed PTB comparison
+ immutable K seal.

```text
python tools/n3_ptb/run_n3_ptb_live.py --min-seals 3 --wait-for-boundary
```

Evidence is written to a **fresh** directory (never appends into prior N1 JSONL).
TLS verification stays on. No orders, auth, or LiveOMS.
