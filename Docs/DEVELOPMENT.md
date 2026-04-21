# Development setup

**Hub:** [README.md](README.md) · **Architecture:** [Architecture.md](Architecture.md) · **Conventions:** [developer_guide.md](developer_guide.md)

How to install, run, test, and iterate on Tyrex_PM locally.

---

## 1. Prerequisites

- **Python 3.11+** (CI / pyproject pin: `requires-python = ">=3.11"`).
- **git** (the runtime stamps the current SHA into `manifest.json`).
- For live mode: `pip install -e .[live]` adds `py-clob-client-v2`, `websockets`, `python-dotenv`.

---

## 2. Install

```bash
git clone <repo-url> tyrex_pm
cd tyrex_pm

python -m venv .venv
.venv\Scripts\activate           # PowerShell:  .venv\Scripts\Activate.ps1
# Linux/macOS:                    source .venv/bin/activate

pip install -U pip
pip install -e .[dev]            # pytest + pytest-asyncio
pip install -e .[live]           # add live CLOB deps when needed
```

The `tyrex-pm` console script becomes available after the editable install (see `[project.scripts]` in `pyproject.toml`).

---

## 3. Run a smoke test (shadow)

```bash
tyrex-pm run \
  --strategy config/strategies/guru_follow.yaml \
  --scenario shadow_guru \
  --run-name dev_smoke \
  --max-iterations 2
```

Inspect the output:

```
var/reporting/runs/dev_smoke/
  manifest.json
  facts.jsonl
  run_summary.json
```

You should see `health(started)` → `guru_poll` → optional `guru_signal` / `intent_created` / `risk_decision` / `oms_submit` → `health(stopped)`. If `guru_poll.guru_wallet_configured=false`, set `strategy.guru.wallet` in your strategy YAML to a real Polymarket wallet, or use `--fixture <path>` to replay recorded data.

---

## 4. Replay a Data API fixture

```bash
tyrex-pm run --strategy config/strategies/guru_follow.yaml \
  --scenario shadow_guru \
  --fixture tests/fixtures/<file>.json \
  --run-name fixture_dev
```

Fixture replay is **shadow only** (live mode rejects `--fixture`).

---

## 5. Tests

```bash
pytest                      # full suite
pytest tests/test_risk_engine.py -k notional   # focused
pytest -k reconcile -x      # bail on first failure
pytest --collect-only -q    # list everything pytest sees
```

`pyproject.toml` sets `asyncio_mode = "auto"` so async tests don't need explicit markers.

Conventions:

- New file naming: `tests/test_<area>_<topic>.py` (mirrors the source layout loosely).
- Place fixtures (JSON, YAML) under `tests/fixtures/` and load via path objects.
- Prefer **golden tests** (full fact stream → expected reason codes) for risk + reconcile changes; the existing suite leans heavily on them.
- Tests must not write into `var/reporting/runs/` — see `tests/test_live_attest_unit.py` for the `_redirect_runs_dir_to_tmp` autouse fixture pattern.

Skip / unstable tests: `TYREX_LIVE_SMOKE=1 pytest tests/test_clob_heartbeat_state_machine.py` opts in to the live heartbeat smoke; otherwise live tests are skipped.

---

## 6. Linting

`pyproject.toml` has a Ruff config:

```toml
[tool.ruff]
line-length = 100
target-version = "py311"
```

Run with whichever Ruff version you have installed (`pip install ruff` if not already):

```bash
ruff check .
ruff format --check .
```

Note: there is no enforced auto-format hook in this repo; keep diffs minimal and consistent with surrounding code.

---

## 7. Live mode locally

1. `pip install -e .[live]`
2. Copy `.env.example` → `.env`, fill `TYREX_PRIVATE_KEY` (and `TYREX_FUNDER` + `TYREX_SIGNATURE_TYPE=1` if you use a proxy / email-wallet).
3. Run a one-shot attestation **before** wiring guru:
   ```bash
   tyrex-pm live-attest --token-id <numeric_clob_token_id> --size 1 --price 0.01 --side BUY
   ```
4. Confirm the run dir contains `live_attest` facts with `outcome=success` and a real `venue_order_id`.
5. Then: `tyrex-pm run --scenario live_guru --max-iterations 5 --run-name dev_live`.

Full operator checklist: [OPERATIONS.md §7](OPERATIONS.md#7-first-time-live-checklist).

---

## 8. Repository conventions

- **No top-level scripts** — entrypoints live under `src/tyrex_pm/runtime/` (mainly `app.py` and `live_attest.py`).
- **Decimal arithmetic everywhere** for money/sizes — never `float`.
- **Time** — use `tyrex_pm.core.time.utc_now()` and `monotonic_s()`; never bare `datetime.utcnow()`.
- **Reason codes** — all denials/skips should map to a constant in `core/reason_codes.py`; add new ones there with a docstring.
- **Reporting facts** — never invent free-form fact types; declare in `reporting/schema_v2.py` first.

---

## 9. Common gotchas

- **Empty `wallet.positions` in shadow** — synthetic fills update the wallet but the bootstrap seeds only USDC; positions accumulate from fills only.
- **Live + proxy wallet "invalid signature"** — set `TYREX_SIGNATURE_TYPE=1` and `TYREX_FUNDER` to the proxy address.
- **`venue_open_not_tracked_locally` blocking** — usually means the venue-adoption matcher couldn't find a matching no-vid local row inside `adoption_grace_s`. Check the `reconcile` fact's `venue_adoption_decisions` for the candidate-match basis.
- **`below_venue_min_size`** — your sizing math produced fewer than 5 shares. Either lift `static_amount_usd`/`copy_scale` so notional clears 5 shares at the limit price, or set `risk.venue_min_size.policy: bump`.
- **Tests failing because `min_notional_usd` was tweaked for a live run** — `config/strategies/guru_follow.yaml` is shared with tests; revert temporary changes before running pytest.
