# Scenario: `layer_a_follow`

Bundled **strategy + risk + runtime** for trying **Layer A** (see `Docs/CONFIG_MODEL.md` § `filters`, `Docs/Implementation/LayerA_Filters/`).

- **Strategy:** `guru_follow.yaml` — **static amount** and **significance conviction (median)** **enabled** at moderate defaults (`amount_usd: 50`); **exit** remains **`mirror_guru`** (`exit_filter.enabled: false`). For **`full_exit`**, see CONFIG_MODEL (fail-closed when context missing).
- **Risk / runtime:** demo caps (per-order / token / portfolio) + capital gate + reserve; guru watermark/dedup under **`var/scenarios/layer_a_follow/`**.

Run (repo root):

```bash
python scripts/run_guru.py \
  --strategy-conf config/scenarios/layer_a_follow/guru_follow.yaml \
  --risk-conf config/scenarios/layer_a_follow/guru_follow_risk.yaml \
  --live-conf config/scenarios/layer_a_follow/live_polymarket.yaml
```

Reporting: inspect **`layer_a_filter`** and **`strategy_decision`** facts for skip/accept reasons.

**Before live use:** set `guru_wallet_address`, tune `filters:` and risk caps, and confirm `token_filter` / allowlist as needed.
