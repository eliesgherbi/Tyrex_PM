# evaluate() lab fixtures (F5)

JSON packs for `tyrex_pm.lab.evaluate_replay`. Schema: `lab_evaluate_v1`.

These are not recordings and not `--live`. Each file is a window plus ordered steps (books, optional L2 depth, optional trades). Steps with `evaluate: true` call the same `driver.evaluate()` as production.

| File | Plugin YAML | Purpose |
|------|-------------|---------|
| `ask70_books.json` | `config/runs/ask70_protection_tiny_live.yaml` | Ask ≥ 0.70 → ENTER |
| `feature_probe_l2.json` | `config/runs/feature_probe_catalog.yaml` | Imbalance in evidence; no entry |
| `z_gap_trades.json` | `config/runs/z_gap_tiny_live.yaml` | evaluate() reachable without gateway |
