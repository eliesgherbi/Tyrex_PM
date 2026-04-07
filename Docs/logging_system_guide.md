# Logging system guide — Tyrex_PM / `run_guru.py`

**Audience:** operators, maintainers, contributors.  
**Scope:** How logging works for the guru follow entrypoint (`scripts/run_guru.py`), what each persisted file contains, and **where new logs should go** so behavior stays consistent.

**Location:** Durable reference at `Docs/` root (alongside [OPERATIONS.md](OPERATIONS.md)). Implementation-phase notes (e.g. [logging_workflow_review.md](Implementation/logging_workflow_review.md), [logging_sources_review.md](Implementation/logging_sources_review.md)) stay under `Docs/Implementation/`.

**Related:** [OPERATIONS.md](OPERATIONS.md) (runbook), [Implementation/logging_workflow_review.md](Implementation/logging_workflow_review.md) (workflow + grepping), [Implementation/logging_sources_review.md](Implementation/logging_sources_review.md) (source map & feasibility), [log_validation_playbook.md](log_validation_playbook.md) (Phase A/B validation procedure).

---

## 1. Purpose

Tyrex uses **several log surfaces**. Two are **persisted by default** (`run_tyrex.log`, `run_nautilus.log`). This document:

- Explains **each source** and its **role**.
- Defines a **stable rule of thumb** for **future development** (where to add new messages).
- Gives operators a **quick reference** for reading files without treating them as a full console transcript.

This is **not** a trading-logic spec; risk and execution semantics live in code and normative Phase A/B docs.

---

## 2. Current logging sources

| Source | Mechanism | Default persistence |
|--------|-----------|---------------------|
| **`print(...)`** | Stdout/stderr from `scripts/run_guru.py` (errors, banner, optional `phase_a:` line, shutdown) | **Console only** — not written to `run_tyrex.log` or `run_nautilus.log` |
| **`tyrex_pm.*` stdlib** | `logging.getLogger(__name__)` under the `tyrex_pm` package | **`logs/<mode>/run_tyrex.log`** via a `FileHandler` on the **`tyrex_pm`** logger (not root) |
| **Nautilus component / kernel / adapter** | Nautilus `Logger` / `self.log` on actors, strategies, engines; framework init | **`logs/<mode>/run_nautilus.log`** via Nautilus **`LoggingConfig`** (`log_directory`, `log_file_name`, `log_level_file`, `clear_log_file`) |
| **Third-party stdlib (e.g. `httpx`)** | Python `logging` to **root** (after `basicConfig`) | **Console only** for typical runs — **not** in `run_tyrex.log` (by design: that handler is not on root) |
| **Startup path lines** | `print` after config load | **Console only** — the two lines `tyrex_pm logging to …` and `nautilus logging to …` |

**`<mode>`** is `shadow` or `live` from runtime YAML **`execution_mode`**.

---

## 3. Role of each source

| Source | What it is for | Typical content |
|--------|----------------|----------------|
| **`print`** | **Operator context** at the boundary of the process: import/dotenv failures, **human-readable** boot summary (mode, trader id, guru prefix, token filter), optional Phase A one-liner, interrupt message | Not grep-structured; high value for “what run is this?” when watching a terminal |
| **`tyrex_pm.*`** | **Tyrex-owned semantics and diagnostics**: compose/Phase B summary, **risk policy** operational lines (`event=tyrex_risk_ops`, gate reasons), cache warmup, data API backoff warnings | Single `Logger` tree; good for **policy decisions** and **Phase B (B2–B4)** explanations that are not emitted through Nautilus `self.log` |
| **Nautilus-native** | **Runtime workflow** inside the trading node: guru poll ticks, **`event=guru_signal_emitted`**, **`event=copy_skip`**, **`reason_code=` / `risk_detail=`**, `live_order_intent`, engine READY noise, adapter chatter | Same **family** as `TYREX-GURU-001.ComponentName` lines on the console |
| **HTTP / root loggers** | Library diagnostics (`HTTP Request: …`) | **Noise** for most Phase A/B questions; kept off `run_tyrex.log` intentionally; use console or future optional HTTP artifact if needed |

---

## 4. File layout and naming

| Item | Rule |
|------|------|
| **Root directory** | Repo root `logs/` (auto-created) |
| **Live** | `logs/live/run_tyrex.log`, `logs/live/run_nautilus.log` |
| **Shadow** | `logs/shadow/run_tyrex.log`, `logs/shadow/run_nautilus.log` |
| **`--log-name NAME`** | `logs/<mode>/NAME_tyrex.log` and `NAME_nautilus.log` (same `NAME` validation as `--help`) |
| **Overwrite** | Each run opens Tyrex and Nautilus targets in **overwrite** mode for that path; default `run_*` names mean “latest run for this mode” |
| **Not captured** | No **full console transcript**; no automatic merge of `print` + Nautilus + httpx into one file |

---

## 5. What each file captures

### `run_tyrex.log`

- **Only** records from the **`tyrex_pm`** logger hierarchy (`%(message)s` format).
- **Includes (examples):** `tyrex_pm phase_b: …` startup summary; `event=tyrex_risk_ops …` lines from `tyrex_pm.risk.configured`; warmup / poller lines from `tyrex_pm.runtime.guru_cache_warmup`, `tyrex_pm.data.data_api_client`; compose module INFO from `tyrex_pm.runtime.guru_compose`.
- **Excludes:** Nautilus `CopyStrategy` / `GuruMonitorActor` lines; raw `httpx` lines; plain `print` banner.

### `run_nautilus.log`

- **Nautilus framework file sink** — formatting and routing are **Nautilus’s**, not Tyrex’s.
- **Includes:** Component/strategy/actor messages you see with trader id prefixes on the console; kernel/engine/adapter INFO at the configured level.
- **Excludes:** Arbitrary Tyrex-only messages that never pass through Nautilus logging (those stay in `run_tyrex.log` if they use `tyrex_pm` loggers).

---

## 6. Development guidance: where future logs should go

Use this **rule of thumb** to keep behavior predictable:

### Prefer `tyrex_pm.*` (`logging.getLogger(__name__)`) when:

- The message describes **Tyrex policy**, **configuration contract**, or **risk/diagnostic** detail that should survive without the full Nautilus stack (e.g. deployment-cap context on `tyrex_risk_ops` denials, data-client backoff).
- You want the line in **`run_tyrex.log`** for **operator grep** alongside Phase B startup text.
- The code path is **not** inside a Nautilus `Strategy`/`Actor` where `self.log` is the established pattern for user-visible flow.

### Prefer Nautilus **`self.log`** (in strategies/actors/exec glue) when:

- The message is part of **per-signal / per-order** **flow** (guru poll, copy skip, intent, submit) and should align with **trader/component** identity on the console and in **`run_nautilus.log`**.
- Operators already grep **`event=guru_*`**, **`event=copy_skip`**, **`correlation_id=`** in component logs.

### When is `print(...)` acceptable?

- **Acceptable:** Irrecoverable **early exits** before logging is configured (**stderr**); **one-shot** human headings that are explicitly **not** part of audit/grep workflows.
- **Prefer logger:** Anything that should appear in **`run_tyrex.log`** for validation (e.g. run identity, Phase A hints) — migrate to **`tyrex_pm`** INFO with stable `key=value` fragments if persistence matters.

### High-signal operator diagnostics

- **Tyrex:** `event=tyrex_risk_ops`, `reason=RISK_*`, `gate=portfolio*`, `tyrex_pm phase_b:`, explicit **`WARNING`** when lenient deployment flags underestimate exposure.
- **Nautilus:** `event=copy_skip`, `reason_code=risk_denied`, `risk_detail=…`, `event=guru_signal_emitted`, `live_order_intent`, execution ERROR lines tied to `correlation_id`.

### Avoid

- **Duplicating** the same semantic event in both **`tyrex_pm` INFO** and **`self.log` INFO** unless one is a deliberate summary and the other is detail (otherwise operators see double counts).
- **Chatty INFO** on hot paths without an `event=` or grep token.
- Attaching new **`tyrex_pm`** handlers for ad-hoc “debug” without going through this guide — use a local script or transient level change when possible.

---

## 7. Operator quick reference

| Goal | Open first | Why |
|------|------------|-----|
| **Trace guru → strategy → skip/submit** | **`run_nautilus.log`** | `GuruMonitorActor`, `CopyStrategy`, correlation ids |
| **Risk gates / deployment caps / tyrex_risk_ops** | **`run_tyrex.log`** | `tyrex_risk_ops`, Phase B startup, risk warnings |
| **Correlate both** | Use **`correlation_id=`** (Nautilus) with **`tyrex_risk_ops`** lines (Tyrex) for the same intent when both fire |

**Reading order:** Many incidents start in **`run_nautilus.log`** (what happened to the signal), then **`run_tyrex.log`** (why policy said no, or deployment-cap detail).

**Console:** Still authoritative for **`HTTP Request`** and any **`print`**-only lines.

---

## Revision history

- **2026-04:** Initial logging system guide (documentation only).
- **2026-04:** Moved from `Docs/Implementation/` to `Docs/` as durable reference.
