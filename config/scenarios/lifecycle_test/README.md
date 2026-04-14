# Scenario: `lifecycle_test`

Reference **strategy + risk + runtime** triple that tracks the **current** repository configuration shape: Layer A filters, Phase B-style risk caps, capital gate, reporting hooks, **explicit** Phase 2 **TradableStateHealth** risk keys, Phase 3 **startup** runtime keys, and an optional **`bot_sell_validate`** block for the validation harness / compose tests.

- **State paths:** `var/scenarios/lifecycle_test/` (watermark, dedup) — isolated from other scenarios.
- **Health gate:** Defaults **`false`**. When **`tradable_state_health_gate_enabled: true`**, compose wires **`NautilusLiveExecutionHealthSource`** (framework startup reconciliation event). Until that event is set, health stays **`UNKNOWN_BOOTSTRAP`** and risk/startup stay fail-closed per §10. See `Docs/Implementation/refactor_lifecycle/tradable_state_health.md`.

Run (repo root; set `guru_wallet_address` first):

```bash
python scripts/run_guru.py \
  --strategy-conf config/scenarios/lifecycle_test/guru_follow.yaml \
  --risk-conf config/scenarios/lifecycle_test/guru_follow_risk.yaml \
  --live-conf config/scenarios/lifecycle_test/live_polymarket.yaml
```

**Shadow tests / smoke:** pair strategy YAML with `guru_follow_risk_shadow.yaml` (no collateral reserve, no finite portfolio cap — required by `validate_phase_b_runtime_contract`).

See **`Docs/CONFIG_MODEL.md`** and **`config/scenarios/README.md`**.
