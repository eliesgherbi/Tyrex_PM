# Shadow validation scenario (`shadow_validation`)

**Purpose:** single bundled **strategy + risk + runtime** triplet for **shadow** smoke runs, report validation, and checking that the **latest** P0–P2 config surface loads and runs **without live orders**.

| File | Role |
|------|------|
| `guru_follow.yaml` | Strategy — replace `guru_wallet_address` before a real run |
| `guru_follow_risk.yaml` | Risk — **shadow-safe** (no finite portfolio / concurrent caps; reserve 0) |
| `live_polymarket.yaml` | Runtime — **`execution_mode: shadow`**; isolated state under `var/scenarios/shadow_validation/` |

**Default run (shadow, no live orders):**

```bash
python scripts/run_guru.py \
  --strategy-conf config/scenarios/shadow_validation/guru_follow.yaml \
  --risk-conf config/scenarios/shadow_validation/guru_follow_risk.yaml \
  --live-conf config/scenarios/shadow_validation/live_polymarket.yaml
```

**Live:** use [`../live_validation/README.md`](../live_validation/README.md) or `config/runtime/live_polymarket.yaml` with a risk profile that matches your test (e.g. `config/risk/guru_follow_risk_phaseb_b2_b3_validate.yaml`). See `Docs/Runbooks/deployment_budget_live_validation.md`.

Production-shaped templates without isolated paths: `config/strategy/`, `config/risk/`, `config/runtime/`.
