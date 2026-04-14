# Scenarios (`config/scenarios/`)

Bundled **strategy + risk + runtime** triples for named deployments and validation. Paths are **isolated** under `var/scenarios/<name>/` (watermark, dedup, etc.) where the scenario YAML sets them, so they do not collide with `config/runtime/` + `var/` defaults.

| Folder | Purpose |
|--------|---------|
| **`layer_a_follow/`** | **Layer A demo** — `filters:` with **static USD floor** + **median significance** on guru BUYs; **`exit_filter`** off (mirror guru). See folder `README.md`. |
| **`lifecycle_test/`** | **Latest template** — same general shape as `layer_a_follow` plus **explicit** Phase 2–3 keys and optional **`bot_sell_validate`**; isolated under `var/scenarios/lifecycle_test/`. **Preferred** for compose/integration tests. See folder `README.md`. |
| **`stabilization_wave1/`** | **Post-WP1 validation** — lifecycle/drain/startup drills; **health gate off**. See folder `README.md`. |
| **`stabilization_wave2/`** | **Post-WP2 validation** — **health gate on** + `NautilusLiveExecutionHealthSource` path. See folder `README.md`. |
| **`stabilization_wave5/`** | **WP5 stabilization validation** — caps, freed deployment/capital awareness, drain, fail-closed reporting. See **`RUNBOOK.md`**. |

Run (replace `<name>`):

```bash
python scripts/run_guru.py \
  --strategy-conf config/scenarios/<name>/guru_follow.yaml \
  --risk-conf config/scenarios/<name>/guru_follow_risk.yaml \
  --live-conf config/scenarios/<name>/live_polymarket.yaml
```

See **[Docs/CONFIG_MODEL.md](../Docs/CONFIG_MODEL.md)** for repository layout and field semantics.
