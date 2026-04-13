# Scenarios (`config/scenarios/`)

Bundled **strategy + risk + runtime** triples for named deployments and validation. Paths are **isolated** under `var/scenarios/<name>/` (watermark, dedup, etc.) where the scenario YAML sets them, so they do not collide with `config/runtime/` + `var/` defaults.

| Folder | Purpose |
|--------|---------|
| **`layer_a_follow/`** | **Layer A demo** — `filters:` with **static USD floor** + **median significance** on guru BUYs; **`exit_filter`** off (mirror guru). See folder `README.md`. |

Run (replace `<name>`):

```bash
python scripts/run_guru.py \
  --strategy-conf config/scenarios/<name>/guru_follow.yaml \
  --risk-conf config/scenarios/<name>/guru_follow_risk.yaml \
  --live-conf config/scenarios/<name>/live_polymarket.yaml
```

See **[Docs/CONFIG_MODEL.md](../Docs/CONFIG_MODEL.md)** for repository layout and field semantics.
