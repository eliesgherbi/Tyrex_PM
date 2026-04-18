# Phase 12 — live parity attestation archive

**Purpose:** Durable, non-secret record that a **real-wallet** `tyrex-pm live-attest` met the §12 binary gate, independent of CI (which stays mock-only for T6).

**Default reporting path** (`config/runtime`): run directories are under `var/reporting/runs/<run_id>/`. The `var/` tree is **gitignored**; this folder holds the **attestation record** and optional **copies** of artifacts.

## Optional: attach a full run bundle

After a successful attest, copy the run directory:

```text
ops/parity_attestation/runs/<run_id>/manifest.json
ops/parity_attestation/runs/<run_id>/facts.jsonl
ops/parity_attestation/runs/<run_id>/run_summary.json
```

Do **not** commit secrets (`.env`, private keys). Facts and summaries are normally safe; review before commit.

## Canonical record

See **`ATTESTATION_RECORD.md`** for the signed-off acceptance criteria, scenario, and HTTP-level confirmation of post + cancel + heartbeat.
