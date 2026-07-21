# N4 OBSERVE runner

Mutation-free OBSERVE composition over N3 sealed PTB + dynamic alignment.

## Fixture mode (N4A / CI)

```text
python tools/n4_observe/run_n4_observe.py --mode fixture --fixture tests/fixtures/n3/n1_three_windows.json --windows 2 --out var/reporting/n4/observe_summary.json
```

## Live mode (N4B — healthy Polymarket TLS host only)

```text
python tools/n4_observe/run_n4_observe.py --mode live --duration-s 180 --max-windows 2 --prep-lead-s 60 --out var/reporting/n4/observe_live_summary.json
```

TLS verification is always on. On a TLS-blocked host classify
`NOT_RUN_ENVIRONMENT_BLOCKED`. Never present fixture output as live evidence.
