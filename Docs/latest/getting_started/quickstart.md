# Quickstart (safe path)

**Purpose:** smallest path that proves the framework runs without real-money risk.  
**Do not** use this page as a live-trading runbook. No new real-money command is provided here.

## 1. CLI identity (read-only)

```bash
cd /path/to/Tyrex_PM
tyrex-pm version
tyrex-pm --help
```

## 2. Offline tests

```bash
pytest
```

## 3. Fixture observe (read-only)

Uses recorded market/reference fixtures — **no venue mutations**:

```bash
tyrex-pm observe --config config/observe_fixture_r3.json
```

Facts land under `var/reporting/` (disposable evidence).

## 4. Optional: live observe / shadow (public data)

These use public market data when configured `mode: live`. They still do **not** submit real orders in OBSERVE / SHADOW:

```bash
# Short public observe (requires network)
tyrex-pm observe --config config/observe_live_r3.json --btc-window next --duration-s 30

# Shadow OMS on public books (paper fills only)
tyrex-pm shadow --config config/observe_shadow_r5.json --btc-window next
```

## Mode distinction

| Class | Examples | Venue mutations |
|-------|----------|-----------------|
| **Read-only** | `observe`, `live-preflight`, `r7c-recon`, `r7-ack-regenerate`, recon scripts | None |
| **Shadow** | `shadow` | None (local ShadowOMS) |
| **Mutation-capable** | `r7b-live-once --execute-live` | Only with explicit operator flag + clean worktree |

Mutation-capable commands exist for historical R7 validation. **R7 live validation is complete.** Do not treat quickstart as authorization for another live run. Future live tests need a new scoped phase — see closed [`../../implementation/r7f_operator_runbook.md`](../../implementation/r7f_operator_runbook.md).

## Next

- Concepts: [architecture](../concepts/architecture.md)
- Modes: [operating_modes](../concepts/operating_modes.md)
- Config: [configuration](../how_to/configuration.md)
