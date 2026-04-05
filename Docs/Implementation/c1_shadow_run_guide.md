# C1 shadow validation (operator guide)

This note supports **real** `rtds_shadow` runs with the **same** `guru_wallet_address` already used in your strategy YAML (for example `run_guru.py`). No separate “test guru” workflow.

## Wallet identity (must understand)

- **Strategy YAML** `guru_wallet_address` is what Tyrex uses to identify the guru on the **poll** path (`GET /activity`) and what you pass to validation tools.
- **RTDS** trade payloads carry **`proxyWallet`** (not `guru_wallet_address`). The stream actor matches **`proxyWallet`** to your configured address **case-insensitively** after normalization.
- **Operational rule:** the normalized forms must be the **same Ethereum address**. If they differ, the stream will show **zero** matched guru events while poll may still work (different identity).

**Startup:** with `guru_ingest_mode: rtds_shadow` or `rtds_primary`, the node logs:

- `guru_rtds_wallet_identity norm=...` (from `guru_compose`) — compare this to what you see as `proxyWallet` on RTDS payloads.
- `event=guru_stream_start ... guru_wallet_norm=... rtds_match_field=proxyWallet` (from `GuruStreamActor`).

Wallet correctness still depends on the guru **actually trading** during the window; a quiet guru yields `matched=0` in the spike and few or no stream logs even when config is right.

---

## A. Wallet validation spike (before shadow)

From repo root, use the **exact** address from `guru_wallet_address` in your strategy YAML:

```bash
pip install -e .
python scripts/spike_rtds_activity.py --wallet 0xYourGuruFromYaml --duration 60
```

Run during a period when the guru is **actively** trading.

**Good:**

- Final line shows **`matched` > 0** (e.g. `done ... matched=12`).
- Sample JSON lines show **`proxyWallet`** equal to your guru address (hex casing may differ; matching is case-insensitive).
- You see **`transactionHash`** and **`asset`** on payloads (used for dedup / signals).

**Bad:**

- **`matched=0`** while you believe the guru traded: wrong wallet, wrong network/account assumption, or quiet period — **do not** trust stream until this is explained.
- **`proxyWallet`** in samples is a **different** address than your YAML — fix config or understanding before shadow.

**PowerShell note:** optional `--filtered-json` can require careful quoting; if in doubt use `cmd`, Git Bash, or skip that flag.

---

## B. Poll-only baseline

In **runtime** YAML (or equivalent config), set:

```yaml
guru_ingest_mode: poll_only
```

Run `run_guru.py` as usual with your strategy YAML.

**Log line to watch:** `guru_signal_emitted` with `source=poll` (poll path is the publishing path in this mode).

This establishes baseline latency and behavior without RTDS.

---

## C. Real shadow run

1. Keep **unchanged** strategy YAML `guru_wallet_address` (same as spike).
2. Set **runtime** `guru_ingest_mode: rtds_shadow`.
3. Run `run_guru.py` normally (same env, risk, logging).

**Compare:**

| Path | What publishes | What to grep / watch |
|------|----------------|----------------------|
| Poll | **Yes** — real `GuruTradeSignal` on the bus | `guru_signal_emitted` ... `source=poll` |
| Stream | **No publish** in shadow — compare-only | `guru_stream_would_emit` ... `correlation_id=...` |

**Good (directional):**

- For trades the guru actually makes, **`correlation_id`** on `guru_stream_would_emit` should **align** with **`source_trade_id`** / correlation on poll emissions when both paths see the same trade (**same** `transactionHash` + **`asset`** leg; dedup id is `transactionHash:asset` when tx is present).
- Stream logs appear **without** long gaps if the guru is active; reconnects recover and do not permanently stall.
- No unexplained **duplicate** downstream behavior beyond what dedup is designed to allow (poll is authoritative in shadow; stream is log-only).

**Bad:**

- Systematic **missing** `guru_stream_would_emit` for trades that produced `guru_signal_emitted` — wallet mismatch, parser drop, or connectivity — **investigate before primary**.
- **Duplicates** or **identity mismatches** (poll id vs stream `correlation_id`) for the same economic leg — **do not** promote to primary.
- Stream **stalls** (no would-emit, no recovery) while poll keeps going — treat as **not ready** for primary.

### After the run (avoid reading the full Nautilus log)

Tyrex logs guru lines to the **Nautilus** file from `run_guru.py` (default `logs/live/run_nautilus.log`, or `logs/live/<--log-name>_nautilus.log`).

Summarize poll vs stream `correlation_id` coverage:

```bash
python scripts/guru_shadow_report.py logs/live/run_nautilus.log
```

Use the printed **`both`** / **`only_poll`** / **`only_stream`** counts, **`stream_first`** vs **`poll_first`** (same id: stream `ts_recv_ms` vs poll `ts_emit_ms`), and **`rtds_guru_events`** (reconnect / stall / fallback). Prefer **`config/runtime/rtds_shadow.yaml`** fresh state so **`only_poll`** is not dominated by one-off historical replay; during overlap, **`both`** should be large for a healthy shadow.

---

## D. When **not** to move to `rtds_primary`

Do **not** run a primary canary until:

- Wallet spike (**A**) is explained and good for the real guru.
- Shadow (**C**) shows acceptable **coverage** and **timing** vs poll, stable reconnect behavior, and no systematic id mismatch.
- You accept residual risk from **manual** judgment (guru activity timing, market conditions).

A short **`rtds_primary`** canary is **only** after the above; keep monitoring and a rollback path to `poll_only` / `rtds_shadow`.

---

## E. Primary soak report (`guru_ingest_mode: rtds_primary`)

After a run, summarize duplicate submits, fallback flapping, gap-fill, and ingest latency from the same Nautilus log as `run_guru.py`:

```bash
python scripts/guru_primary_report.py logs/live/run_nautilus.log
```

Exit code **2** if it finds duplicate `guru_signal_emitted` rows for the same id+source, duplicate `live_order_submit` per `correlation_id`, or the same correlation id emitted from more than one source (`rtds` vs `poll` vs `gap_fill`). **Host-level** RTDS load is not in the log; check CPU / memory / network yourself.

---

## Dedup note (shadow + primary)

`source_trade_id` / stream `correlation_id` use **`transactionHash:asset`** when `transactionHash` is present so **two legs** in one tx (different `asset`) are **not** collapsed by dedup. Fallback without tx remains the deterministic composite (timestamp, asset, side, size, price).
