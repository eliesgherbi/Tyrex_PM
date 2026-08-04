# Quickstart (safe path)

**Purpose:** smallest path that proves the framework runs without real-money risk.  
**Do not** use this page as a live-trading runbook. No new real-money command is provided here.

## Operation effects (short)

| Effect | Meaning |
|--------|---------|
| Offline | No network |
| Network read | GET/stream only |
| Report write | Writes under `var/runs/` |
| Local state write | Writes under `var/runtime_state/` |
| Venue / on-chain mutation | Orders or chain ops |

## 1. CLI identity (offline)

```bash
cd /path/to/Tyrex_PM
tyrex-pm version
tyrex-pm --help
```

## 2. Offline tests

```bash
pytest
```

## 3. Fixture observe (offline + report write)

Uses recorded fixtures — **no network**, **no venue mutation**; writes facts under `var/runs/`:

```bash
tyrex-pm observe --config config/observe_fixture_r3.json
```

## 4. Optional: public observe / shadow (network read)

Public market data when `mode: live`. **No venue order mutation.** Shadow may write a local snapshot under `var/runtime_state/` if configured; both write facts under `var/runs/`.

```bash
tyrex-pm observe --config config/observe_live_r3.json --btc-window next --duration-s 30
tyrex-pm shadow --config config/observe_shadow_r5.json --btc-window next
```

## Mode distinction

| Class | Examples | Venue mutation | Other effects |
|-------|----------|----------------|---------------|
| Offline | `version`, fixture `observe`, `pytest` | no | report write for observe |
| Network read | live `observe`, `shadow`, `live-preflight`, recon scripts | no | report write; shadow may local-state write |
| Local state write (no venue) | `r7-ack-regenerate` | no | network read + ack artifact write |
| Venue mutation | `r7b-live-once --execute-live`, `n7-live --live` | **yes** | **not** part of quickstart |

**R7 live validation is complete** (closed runbook).  
**N7 Z-Gap tiny live** is the current operator-gated Scope A path — see [run_modes](../how_to/run_modes.md) (fake / RO first; `--live` is operator-only).

Safe N7 rehearsals (no venue mutation):

```bash
python tools/n7_live/run_n7_live_oneshot.py --fake-rehearsal
tyrex-pm n7-preflight
```

## Next

- Concepts: [architecture](../concepts/architecture.md)
- Modes: [operating_modes](../concepts/operating_modes.md)
- Config: [configuration](../how_to/configuration.md)
- Run recipes: [run_modes](../how_to/run_modes.md)
