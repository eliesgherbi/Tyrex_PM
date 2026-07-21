# N2 public-data smoke

Mutation-free connectivity check for N2 adapters. TLS verification is always on.

```text
python tools/n2_smoke/smoke_public_feeds.py --duration-s 28 --out var/reporting/n2/smoke_summary.json
```

Connectivity diagnosis (no TLS bypass):

```text
python tools/n2_smoke/diagnose_connectivity.py
```

Writes `var/reporting/n2/connectivity_diagnosis.json` (gitignored).
