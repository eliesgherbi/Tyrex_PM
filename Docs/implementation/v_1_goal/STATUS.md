# V1 status and decision log

Last update: 25 October 2026. Current behaviour is described in [`Docs/latest/`](../../latest/README.md); this page records what was decided and delivered, in order.

## Where v1 stands

| Area | State | Evidence |
|---|---|---|
| One strategy contract (`StrategyDriver`, `BaseDriver`, triggers, one context builder) | Done | `tests/unit/strategies/test_driver_contract.py` |
| Trading host split into components (`runtime/host/`) | Done | `tests/unit/runtime/test_host_component_sizes.py`; full suite unchanged |
| One replay path for every strategy; shared fill rule with paper | Done | V1-a `tests/integration/test_v1_all_strategies_replay.py`; V1-b `tests/unit/execution/test_fill_parity.py` |
| Shared features and readiness (`gates_entry`, `tau_s`, trade size/side) | Done | strategy and runtime suites |
| Market families from configuration | Done for BTC up/down windows | V1-d `tests/unit/runtime/test_market_family_config.py` |
| Research workspace for any strategy | Done | V1-e `tests/unit/research/test_workspace_any_strategy.py`; browser check (z_gap and ask70 validated, a real z_gap job run and opened) |
| A new strategy without framework edits | Done | V1-c `tests/integration/test_v1_new_strategy.py` |
| settlement_value results unchanged by the consolidation | Done | `tools/v1_parity_check.py` on jobs `01777c96…` and `55815cab…`: 64/64 units equal after every step |

Commits on `rest_project`: `a7a06c4` (cleanup), `0e8162f` (frontend), `24e90ec` (step 1), `687b0ad` (step 2), `bba92fd` (steps 3–4), `29eae60` (step 5), `eae482a` (step 6), then this documentation. Removed code is reachable at tag `archive/pre-v1-2026-10-24`.

## Decisions (in order)

1. **23 Oct — data policy.** Window sets use `replay_research_v2_1` (books valid at least 90%, no hole over 20 s, none in the last 60 s, blips under 250 ms ignored, reference covering the window, provider-reported winner). K comes from the exact start tick, checked later against the provider's price to beat. Default scenario `friction_v3` with `venue_top_v1` books; stress `friction_stress_v2`. Detail: [replay_data_policy_review_2026-10-23.md](replay_data_policy_review_2026-10-23.md) (sections 1–8 are the analysis; the defaults there were superseded by this item).
2. **23 Oct — parser version 2.** The metadata minimum order size is recorded in shares; version-1 builds stay for pinned sets.
3. **23–24 Oct — Data Library.** Delete to trash, set versions, reservations, export, recording from the page with finishing the last window and waiting for winners, fetching missing winners, the set cookbook. Detail: [data_library_enhancement_proposals_2026-10-23.md](data_library_enhancement_proposals_2026-10-23.md) (its "implemented" sections).
4. **24 Oct — framework consolidation.** Plan: [v1_framework_consolidation_plan.md](v1_framework_consolidation_plan.md). Re-scoped while implementing:
   - the fixture lab stays a decision harness (synthetic packs cannot pass live readiness rules);
   - the engine's scripted mode stays as the simulator's own test harness;
   - unused `Scenario` fields stay because saved experiment snapshots carry them;
   - no separate `models/` package (settlement_value already uses the shared fair value; its probability bands and sizing are strategy choices);
   - no framework-wide episode state (the other strategies keep a single flag).
5. **24 Oct — one crossed-book rule.** Best bid at or above best ask is invalid, live and recorded; under `venue_top_v1` an hour of books (6.1 million checks) had no such book.
6. **25 Oct — market families.** Enabling a family is a normalizer change and goes with a parser version bump; families for other assets are refused until their feeds are wired.

## Open items

| Item | Why open |
|---|---|
| Live run of the recording closing phase and of fetching winners | Needs a real recording (a 24 Oct recording may already cover the closing phase) |
| Binance trade side in recordings | `trade_imbalance` in replay needs the normalizer to keep the side: parser version 3 and a re-normalization of several hours |
| Retiring parser-1 builds | They double the normalized footprint; retire once no saved set or study uses them |
| Other assets (ETH) | Collector and runtime feed wiring (supervisor, streams, RTDS symbol) |
| Tune | Next research phase (parameter search with a reserved holdout) |

## Earlier documents in this folder

`01_*` to `04_*`, `step_*`, `02_step2_follow_up.md` and [research_IHM_enhancment.md](research_IHM_enhancment.md) are the history of how the workspace was specified and built. They are not current plans; where they disagree with `Docs/latest/` or this page, they are superseded.
