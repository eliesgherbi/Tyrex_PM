# N5 SHADOW tools

## N5A (fixture / offline)

```powershell
python tools/n5_shadow/run_n5_shadow.py --mode fixture
```

Default config: `config/observe_shadow_z_gap_n5a.json` (`shadow_depth_walk_v1`).

## N5B (live inputs, SHADOW execution only)

Public market data only. Execution is structurally SHADOW (`ShadowOMS` +
`shadow_depth_walk_v1`). No LiveOMS, auth, or venue mutation.

```powershell
python tools/n5_shadow/run_n5_shadow_live.py --mode live --wait-for-boundary --duration-s 1100 --min-seals 3 --out var/reporting/n5b/shadow_live_summary.json
```

Config: `config/observe_shadow_z_gap_n5b_live.json`.
