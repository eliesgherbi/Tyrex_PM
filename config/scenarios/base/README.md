# Scenario: `base`

Shared **template** runtime YAMLs for experiments. State files default under **`var/scenarios/base/`** (create the directory if missing).

**Prefer** **[`../venue_state_live/`](../venue_state_live/)** for documented VenueState + wallet-sync validation and explicit `venue_state_*` keys.

## Files

| File | Role |
|------|------|
| `live_polymarket_live.yaml` | Live execution example with wallet sync + VenueState defaults (via loaders when keys omitted). |
| `live_polymarket_shadow.yaml` | Same shape; adjust `trader_id` / phase tags for your run. |

## Quick start

```bash
python scripts/run_guru.py \
  --strategy-conf config/scenarios/base/guru_follow.yaml \
  --risk-conf config/scenarios/base/guru_follow_risk.yaml \
  --live-conf config/scenarios/base/live_polymarket_live.yaml
```

See **`RUNBOOK.md`** for where current validation lives (pointer to **`venue_state_live`**).
