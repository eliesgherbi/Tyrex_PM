# Migration map (Z-Gap active scope)

**Status:** Draft from Phase 0 audit  
**Rule:** Keep / adapt / move / replace / remove only after parity evidence  
**Engine column:** Tentative until `nautilus_decision.md` is final

Action legend: **Keep** · **Adapt** · **Move** · **Replace** · **Remove** · **Investigate** · **Out of scope**

| Current module / file | Current role | Target owner | Action | Parity evidence |
|-----------------------|--------------|--------------|--------|-----------------|
| `strategies/z_gap/entry_eval.py` | Entry gates | `strategies/z_gap` | Keep | Existing unit tests |
| `strategies/z_gap/exit_eval.py` | Exit precedence | `strategies/z_gap` | Keep | Existing unit tests |
| `exit_policy/thesis_stop.py` | Thesis stop | `strategies/z_gap` exit policies | Move (package boundary) | `test_z_gap_thesis_stop.py` |
| `strategies/z_gap/entry_plan.py` | Sizing/limit plan | `strategies/z_gap` | Keep | Tests |
| `strategies/z_gap/exit_plan.py` | Exit intent build | `strategies/z_gap` | Keep | Tests |
| `strategies/z_gap/lifecycle.py` | Phase transitions | strategy + lifecycle host | Adapt | Tests |
| `strategies/z_gap/ptb_policy.py` | PTB source select | `domain/polymarket` or z_gap | Keep/Adapt | PTB tests |
| `strategies/z_gap/facts.py` | Fact emitters | `reporting` + strategy | Adapt | Schema tests |
| `strategies/z_gap/strategy.py` | OMS duck-type handle | Real Strategy contract | Adapt | After Phase 2 |
| `strategies/z_gap/shadow_harness.py` | Shadow E2E | operations/tests | Keep | shadow e2e |
| `strategies/z_gap/scenario_oms.py` | Test OMS | tests | Keep | |
| `quant/*` | σ, FV, fees, edge, sanity | strategy lib | Keep | Do not change math |
| `runtime/z_gap_run.py` | Observe loop + tick | application/operations host | Move/slim | Observe parity |
| `runtime/z_gap_enforce.py` | Enforce loop | same host + intent dispatch | Move/slim | Shadow/live parity |
| `runtime/z_gap_session_orchestrator.py` | Second control plane | Merge into one host | Adapt/Merge | Control-path unification |
| `runtime/z_gap_session_runtime.py` | Incomplete bootstrap | Merge with Path A feeds | Adapt | Must start books+user+ledger |
| `runtime/z_gap_boundary_gate.py` | Boundary readiness | operations | Keep/Move | |
| `runtime/z_gap_ptb_*.py` | Capture/attest/commission | domain PTB service | Keep/Move | Commissioning reports |
| `runtime/z_gap_live*.py`, `z_gap_preflight.py` | Enforce gates | operations | Keep/Move | |
| `runtime/z_gap_sigma_warmup.py` | Pre-boundary σ | strategies/z_gap | Move | |
| `runtime/z_gap_interactive_approval.py` | Operator approve | operations | Keep | |
| `runtime/z_gap_sidecar_supervisor.py` | Sidecar health | operations | Investigate | |
| `runtime/z_gap_artifact_writer.py` | Artifacts | operations/reporting | Keep | |
| `runtime/z_gap_config_hash.py` | Config hash | operations | Keep | |
| `runtime/z_gap_experimental.py` | Experimental modes | operations | Adapt | Collapse modes |
| `runtime/z_gap_clock_sanity.py` | Clock artifact | operations | Keep | |
| `runtime/z_gap_model_facts.py` | Model facts helper | reporting/strategy | Move | |
| `runtime/signal_feed_runtime.py` | Binance/RTDS/PTB wiring | adapters + domain | Adapt | Shared by strategies |
| `runtime/time_authority.py` | Clock authority | engine/ops | Keep | Critical invariant |
| `runtime/btc_5m_metadata.py` | Instrument resolve | domain/polymarket | Keep | |
| `runtime/pipeline.py` | Intent→risk→OMS | engine (Tyrex or bridge) | Keep/Replace* | *Replace only if NT adopted |
| `runtime/run_once.py` Z-Gap branch | Path A entry | application | Slim | One path |
| `runtime/config.py` Z-Gap parsers | Mega-config | Split strategy config | Adapt | Later |
| `risk/*` | Fail-closed risk | engine | Keep/Adapt | |
| `execution/*` | Planner + OMS | engine | Keep/Replace* | |
| `state/market_store.py` | Book truth | market_data/engine | Keep/Replace* | |
| `state/signal_state_store.py` | Ref prices | domain/market_data | Keep | |
| `state/z_gap_ptb_store.py` | PTB lock | domain PTB | Keep | |
| `state/order_store.py`, `wallet_store.py`, `allocation_ledger.py` | Portfolio truth | engine | Keep/Replace* | |
| `ingestion/price_to_beat_tracker.py` | PTB observe | domain | Keep | |
| `ingestion/btc_5m_window_scheduler.py` | Window select | operations | Keep | Shared primitive |
| `venue/polymarket/*` | CLOB adapters | adapters | Keep/Replace* | |
| `venue/binance_data/*` | Binance | adapters | Keep/Replace* | |
| `venue/polymarket_rtds/*` | RTDS CL+Binance | adapters | Keep (esp. Chainlink) | Track C |
| `market_data/*` | Quality/VWAP | market_data | Keep | |
| `reporting/schema_v2.py` Z-Gap facts | Observability | reporting | Keep | |
| `scripts/go_z_gap_tiny_live.py` | Operator CLI | Thin → `tyrex-pm` | Adapt | No trading logic |
| `scripts/run_z_gap_observe_batch.py` | Batch scheduler | operations | Keep/Adapt | |
| `scripts/run_z_gap_shadow_e2e.py` | Shadow proof | tests/ops | Keep | |
| `protection/*`, `survival/*` | Other strategies | — | Out of scope | Do not import into Z-Gap |
| `strategies/paired_binary/*` | Other product | — | Out of scope | Track E read-only |
| `strategies/guru_follow/*` | Other product | — | Out of scope | |
| `research/z_gap/*` | Calibration offline | research | Keep | |

\*Replace only after NT PoC pass and parity attestation.

## Unification priority (before Phase 3)

1. Single bootstrap that always wires: books + signal feeds + PTB + clock + (enforce) user WS + allocation.  
2. Single operator entry invoking that bootstrap.  
3. Relocate observe/enforce tick orchestration behind one host that calls strategy callbacks + intent dispatcher.  
4. Retire the weaker duplicate path only after parity.
