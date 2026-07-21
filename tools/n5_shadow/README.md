# N5A SHADOW tools

`run_n5_shadow.py` validates and instantiates the offline deterministic N5A
composition, then writes a fixture-only summary JSON:

```powershell
python tools/n5_shadow/run_n5_shadow.py --mode fixture
```

The default configuration is `config/observe_shadow_z_gap_n5a.json`. It enables
`shadow_depth_walk_v1` with explicit simulated latency, requires a flat
portfolio before prepared-next promotion, and leaves resolution capability off.

N5B live operation is deferred. This directory contains no command that calls
live endpoints or submits venue orders.
