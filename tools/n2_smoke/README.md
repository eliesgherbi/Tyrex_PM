# N2 public-data smoke

Mutation-free connectivity check for N2 adapters.

```text
python tools/n2_smoke/smoke_public_feeds.py --duration-s 28 --out var/reporting/n2/smoke_summary.json
```

Optional `--insecure-ssl` is diagnostic-only for broken TLS interception.
Not for production.
