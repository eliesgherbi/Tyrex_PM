# Task A implementation plan: terminal-probability ML lab

20 October 2026. **Proposed plan, not implementation.** This is the current scope for the ML work and supersedes the initial sequencing in the [15 October proposal](btc5m_learned_strategy_research_2026-10-15.md). It is grounded in the present working tree, including uncommitted framework changes. No notebooks, fitted models, strategy behavior or live settings are delivered by this document.

## 1. Objective and scope

Build a small, understandable research workflow that prepares data, trains a model and evaluates:

`p_up(t) = P(the market's terminal Up outcome wins | information available at t)`

For a verified binary Up/Down outcome, `p_down = 1 - p_up`. The first model is a regularized logistic classifier. The goal is to learn the complete dataset → training → evaluation → saved inference artifact lifecycle before adding model complexity.

Later, plug the fitted probability source into the **existing `settlement_value` strategy**. Preserve its entry, side-selection, sizing, costs, retry, exit and execution rules in the selected comparison profile. We do not need another strategy implementation to test another probability generator.

The first integration comparison uses a frozen hold-to-resolution profile to isolate the forecast replacement. Existing managed-exit capabilities remain available for later experiments. Short-horizon quote forecasting, new exit policies, re-entry research, deep learning and RL are outside this first scope.

**Limited data does not block building the research workflow.** It limits the conclusions we may draw. The same code should accept more compatible recordings through configuration, produce a new immutable dataset, and rerun training and evaluation. More data does not automatically fix label semantics, feed differences, distribution changes or an already-used test set.

## 2. Current framework: what changed since 15 October

The earlier controller-first recommendation is no longer a prerequisite:

- `settlement_value/driver.py` now contains entry/exit feedback, retry and episode state, durable-usage restoration, re-entry logic and managed value exits.
- `settlement_value/plugin.py` selects hold or managed-exit lifecycle compatibility from configuration.
- `research/strategy_replay/run.py` now routes exits, applies SELL feedback and reports execution outcomes to the driver.
- The live host has continuous-run support and same-window terminal-session release paths.
- Shared recording schemas, raw-input capture/import, recorded execution context and causal decision reconstruction provide stronger integration evidence.
- Reference availability, volatility time policy, disagreement veto and book reconstruction are versioned experimental behavior. They must be pinned in dataset/model/comparison identities.

These are implemented mechanisms, not a declaration of live qualification. The [20 October status report](../implementation/strategies/trading_integrity_and_managed_exit_status_2026-10-20.md) still records incomplete full trading-capture reconstruction and public-feed paper qualification. None of that blocks offline ML dataset and training work.

Two gaps remain directly relevant to ML: there is no terminal-model `ml_lab`, and probability selection is still hard-coded in `SettlementValueDriver._observe()` between analytical backends. That selection is the later integration seam.

## 3. Organization and ownership

Proposed source layout:

```text
src/tyrex_pm/
  research/
    strategy_lab/                  # existing strategy replay/economics experiments
    ml_lab/
      __init__.py
      README.md
      ai_settlement/               # task/model family, not one folder per algorithm
        __init__.py
        README.md
        experiment.json            # sources, feature/sampling policy, split intent
        00_data_preparation.ipynb
        01_train_and_evaluate.ipynb
        dataset.py                 # inventory, extraction, labels, immutable builds
        splits.py                  # chronological grouped partitions and purging
        training.py                # training-only fitting and experiment records
        evaluation.py              # forecast metrics and plotting helpers
  models/
    settlement_probability/        # small reusable inference boundary
      __init__.py
      features.py                  # pure feature calculation and schema
      predictor.py                 # artifact loading and predict interface
```

These paths are proposals. Keep notebooks explanatory and executable from a fresh kernel. Put substantive preparation/training logic in ordinary modules so it can be tested and rerun without notebook state. Do not build a general ML platform, model service or broad model registry for the first model.

The shared `models/` boundary contains only feature/inference code. Production must not import research notebooks, dataset builders or plotting modules. Offline data adapters translate recorded observations into the same causal feature inputs that a later live adapter supplies.

Generated outputs live under `var/research/ml_lab/ai_settlement/`, outside notebook source:

```text
datasets/<dataset-id>/
  manifest.json
  windows.parquet
  features.parquet
  labels.parquet
  splits.json
  quality_report.json
runs/<run-id>/
  manifest.json
  model.joblib
  metrics.json
  validation_predictions.parquet
  report.html
  freeze.json                      # when a candidate is frozen
  test_predictions.parquet         # only after explicit frozen evaluation
  handoff.json
```

The first folder is `ai_settlement`; algorithm IDs such as `logistic_v1` belong in experiment/run metadata. A future tree model should reuse the dataset rather than duplicate source reconstruction. Package the notebooks as research assets if installed-package access is intended; current `pyproject.toml` includes tutorial notebooks but no ML lab assets yet.

## 4. Notebook 00: understand, prepare and validate the dataset

### 4.1 Explain the sources before transforming them

Start with an inventory of configured recordings, dates, sessions, market/window IDs, source-stream identities, build/checksum evidence and available fields. Read the existing normalized warehouse using its reader and reconstruction utilities. Original raw messages remain provenance; notebook cells should not reimplement normalization.

Existing captured runs can enter through `capture_replay` import into their own warehouse collections. They are optional additional sources, not a new mandatory recording system. Keep PolyBolt and RTDS reference streams distinguishable; do not pool them as equivalent without a declared compatibility policy.

Describe the required inputs in plain language:

- **Market metadata:** identity, Up/Down mapping, opening/end times and rules identity.
- **Opening threshold K:** value, quality, provenance and when it became available.
- **BTC/reference observations:** values, source and receipt times, clock state and connection generations.
- **Volatility:** the existing causal estimator with its exact configuration and reset policy.
- **Resolution evidence:** terminal label and its provenance, available only retrospectively.
- **Books:** quote quality and market-probability diagnostics; not initial learned features.

Reuse `WindowCatalog`, bounded loading, routing, indicator and reference-availability behavior where applicable. Extract observations through a small extraction-only adapter over that stack; do not run the execution engine, generate trade intents or call the private strategy `_observe()` to manufacture training rows.

### 4.2 Define one training example explicitly

One row represents one market at one decision time, sampled on a declared one-second grid inside its five-minute window. Each row names market/window, collection/session, receipt cutoff, corrected decision time, feature lookback, source identity and feature readiness.

Only observations actually received by the cutoff can contribute. Corrected/source time does not make a message available before its receipt. Respect historical/subscription delivery policies, clock uncertainty, reconnect resets, warm-up and stale/missing inputs. Do not borrow a later sealed K or revised metadata for an earlier row.

Keep the full sampling grid with eligibility/rejection reasons so coverage is visible. Train only on eligible, labeled rows. Group duplicate recordings of the same market, including imports of the same capture, into one declared source selection; never count them as independent windows.

### 4.3 Label terminal Up, not a proxy price movement

Maintain a separate market-level label table: `market_id`, `y_up`, `label_type`, method/version, payout/outcome evidence, first availability time and status. Join labels to examples after causal feature extraction.

- Qualified evidence may supply the official binary label.
- Existing `strategy_replay/labels.py` supplies explicitly named **proxy outcome** semantics for diagnostic research: one positive payout entry with a known outcome mapping.
- Unknown, contradictory, split or missing outcomes remain unlabeled, with their reasons.
- Do not derive the winner from Binance close minus open, the last quote, or the analytical model being replaced.

A proxy-label mode is acceptable for building and testing the pipeline. Its artifact and reports remain `research_only`; it cannot establish actual-contract predictive quality. Do not silently combine qualified and proxy labels into a single reported result. Existing `proxy_outcome()` is useful for method reuse, but audit label availability and any later contradictory evidence separately rather than assuming a timeless winner.

### 4.4 Keep the first feature set small

Proposed `terminal_features_v1`:

- Log distance between the configured current spot/reference input and the known K.
- Remaining seconds to the declared window end.
- Causal volatility estimate in the existing per-square-root-second units.
- Distance normalized by volatility and remaining time, with an explicit time-floor policy.

Compute deterministic transformations through the shared feature function. Persist units, order, source choice and estimator policy. Missing/invalid required values make the row ineligible; do not substitute zero. These deliberately simple features will establish plumbing and a benchmark, not guarantee an improvement on diffusion pricing.

Record analytical endpoint/TWAP predictions and usable book-implied probability alongside the rows as **baselines/diagnostics**, with separate availability masks. They are not implicitly added to the feature list. Book or flow features are later named experiments.

Do not fit scalers, choose supervised features, or perform label-guided selection on the complete dataset in this notebook. Fitted transformations belong to training.

### 4.5 Visualize data suitability

Explain counts of windows and sessions as well as rows. On the development/training portion show:

- Eligible rows and missingness by input, window and remaining-time bucket.
- Gaps, warm-up losses, threshold availability and reconnect examples.
- Target balance by market and calendar block; counts of unavailable/proxy labels.
- Feature ranges, outliers, distributions, dependence and correlation.
- Terminal outcomes versus distance-to-K and remaining time, with group counts.
- A few timelines connecting received observations, feature values and the eventual outcome.

These are descriptive checks, not proof of predictive information. Do not inspect final-test label relationships or use test plots to choose transformations. Test-side integrity, date and partition-membership checks remain permissible. For sparse groups, show counts and suppress unsupported curves rather than draw reassuring smooth estimates.

### 4.6 Freeze train, validation and test partitions

Use chronological whole-market groups, preferably separated by session/day when coverage permits. The validation block supports choices and optional calibration; the later test is reserved for a frozen candidate. All times belonging to one terminal outcome stay in one partition.

Purge training labels/lookbacks that overlap the next partition under a named conservative interval policy. Require every training label to have been available by the simulated training cutoff. Record purged/excluded rows. Fit no transformation using validation or test values.

The previously used S2/P7 holdout is **not a fresh ML test set**. Existing research windows can develop this workflow, but any claimed independent evaluation needs genuinely unexamined later windows. Record prior exposure in `splits.json`; if none exist, export a development/demo split with `independent_test_available: false`.

Give each market equal total training weight so a window with more eligible seconds does not dominate automatically. Report market-weighted primary metrics and row-level secondary metrics. Do not use class balancing by default: the task estimates the physical outcome frequency, and reweighting classes changes that interpretation.

**Notebook 00 output:** an immutable, checksummed dataset with explicit feature, label and partition contracts. If the data cannot support a meaningful fit/split, it still produces a useful quality report and a named limitation; it must not fabricate a row-random test.

## 5. Notebook 01: train, evaluate and export

### 5.1 Read a frozen dataset

Load a specific dataset ID and verify its manifest. Display the task, source restrictions, feature schema, label status, market counts and split exposure before fitting. Never reconstruct a different dataset implicitly from the current warehouse while reusing the old run ID.

### 5.2 Establish baselines, then fit one model

Compare a training-only constant probability, the existing analytical forecasts and the usable Up midpoint as a market diagnostic. Report each baseline's coverage and common eligible subsets; do not manufacture missing baseline predictions or compare unlike row sets without saying so.

Fit one pipeline: training-only standardization → regularized logistic regression. Fix the first feature list and regularization setting; avoid a broad search. Save the fitted preprocessing with the model. If training contains only one class, report a baseline-only demonstration rather than claim a trained binary classifier.

Show training and validation log loss/Brier, solver convergence, predicted-probability distributions and standardized coefficients. Logistic regression does not naturally produce a neural-network epoch history: do not invent training-loss curves. A learning curve by the number of earlier training windows can be added only when the sample supports it.

### 5.3 Evaluate the probability forecast

Primary metrics: log loss and Brier score. Accompany them with reliability bins/counts, sharpness, and slices by remaining time, distance-to-K, source/session and input quality. Accuracy and ROC-AUC are secondary and may be unavailable for single-class subsets. Lower Brier loss alone does not prove improved calibration.

Quantify uncertainty at market/session/calendar-block level where possible. Hundreds of seconds in one market are dependent observations sharing one target. Sparse results remain descriptive. No PnL, fill quality or readiness for funded trading follows from this notebook.

Keep calibration off for the first fit unless independent development data supports it. If introduced, reserve an explicit later calibration block or use chronological out-of-training predictions; do not use the test set or default random-fold calibration. Refit rules and weighting are recorded.

### 5.4 Separate iteration from the final test

Default Run All trains and evaluates on validation. It does not repeatedly consume the reserved test block. Freeze features, preprocessing, model, any calibration and evaluation criteria before enabling the final-test section. Save the freeze and record test access.

After inspecting test results, any further model choices belong to a new development experiment; that block can no longer be presented as untouched. A fresh later test is needed for a new independent claim. A tiny exposed-data run is still valid pipeline engineering if labeled accordingly.

### 5.5 Export one self-contained inference artifact

Bundle model and preprocessing, class mapping (`P(Up)` must select the correct class), feature order/units, task/label policy, accepted source/reference/rule versions, missingness behavior, training cutoff and dataset/split/code identity. Pin numerical-library versions and checksums. Use a trusted local pipeline serialization for the prototype; package loading must not rely on an active notebook or refit any component. A slim inference dependency extra can be added during integration so Jupyter/plotting dependencies do not become live requirements.

Export small reference feature/prediction cases to verify save/load equivalence. A failed or weak model may still be saved as `research_only`; export is not promotion.

The handoff distinguishes `pipeline_verified`, `research_only`, `ready_for_replay_candidate` and the supporting evidence. A replay candidate needs a valid reproducible artifact, causal inputs, explicit label/source restrictions and a reviewed forecast report. Lack of statistical power can keep it diagnostic even when the software is correct. No hard profitability or live-readiness flag is inferred from classification metrics.

## 6. Later integration: replace the probability source in the same strategy

This stage follows the two notebooks. `strategy_lab` remains the place to evaluate trading behavior.

Add a small predictor boundary at the current `_observe()` probability-selection point. Proposed backend name: `learned_terminal`. Keep the analytical backends and diagnostics available; extend configuration validation explicitly rather than bypassing the existing parser or monkeypatching a notebook into the driver.

The shared inference adapter consumes causal feature inputs and returns readiness/reason, `p_up`, artifact/schema identity and as-of validity. Loading happens before decisions, once per run/artifact; inference performs no training, file reads or network calls. Invalid, stale, nonfinite, incompatible-source or incompatible-rule output blocks new ML-based entry. Do not silently fall back to another probability generator.

### The point/band distinction is an explicit integration gate

Current `ProbabilityEstimate` supplies `p_up_point`, `p_up_low`, `p_up_high` and `interval_method`. Entry sizing uses `p_low` for Up and `1 - p_high` for Down; some managed exits use an upper bound. Therefore a classifier's point estimate alone is not the full strategy input.

Train and assess point probabilities first. Before strategy replay, implement a named conservative wrapper. The simplest research option is an explicit safety buffer `delta`, giving `low = max(0, p - delta)` and `high = min(1, p + delta)`. Its value must be fixed through development and recorded as an **operational decision buffer**, not a confidence interval. This is a proposal, not an approved numerical setting.

For a matched forecast-replacement comparison, apply the same frozen buffer convention to analytical and learned point predictions. Keep the original analytical sensitivity-band run as a separate existing-system reference; changing band conventions changes experimental behavior and must have a new identity. Alternatively, retain the existing sensitivity-envelope approach through a properly defined predictor wrapper, as a separately declared experiment. Never attach the old analytical bounds to an unrelated ML point or silently collapse all bounds to the point.

The strategy's entry/side/sizing/execution logic stays shared. The probability artifact and its explicitly versioned conservative treatment are the changed inputs. Freeze the hold profile's other parameters, execution/reference/book policies, budget and windows for comparison.

Verify artifact round-trip and offline/live feature parity first. Replay a frozen candidate in `strategy_lab`; then use read-only shadow inference on new captures; only later consider paper and separately authorized funded trials. Existing accounting, gates, protections and metadata requirements remain authoritative.

## 7. Work packages and acceptance criteria

1. **Define task/source contracts and inventory.** Deliver experiment configuration, label policy, feature dictionary and split-exposure report. Current data must suffice to demonstrate mechanics; no claim of sufficient training coverage is required.
2. **Implement dataset helpers and Notebook 00.** Acceptance: causal rows, preserved missing labels, stable source selection, deduplication, grouped chronological splits, clear quality plots, immutable manifests and rerun equivalence.
3. **Implement training helpers and Notebook 01.** Acceptance: one fixed logistic pipeline, train-only transforms, baseline comparisons, validation diagnostics and honest small-data behavior.
4. **Freeze/evaluate/export a candidate.** Acceptance: explicit test-access policy, metrics with market counts and label provenance, versioned artifact, save/load equivalence and handoff status. A diagnostic run can complete without a qualified independent test.
5. **Integrate the probability backend.** Acceptance: shared feature/inference code, parser support, documented band semantics, recorded artifact identity, fail-closed compatibility checks and no strategy-policy fork.
6. **Evaluate trading separately.** Acceptance: analytical/ML comparisons through existing `strategy_lab`, then operational parity/shadow/paper evidence before any funded decision.

Meaningful implementation checks include feature-prefix invariance when future rows are appended, unavailable-K handling, no train/test market overlap, label-availability cutoffs, training-only scaler statistics, invalid-artifact rejection and identical predictions after reload. Execute both notebooks from clean kernels on a small pinned fixture; put executed copies and receipts under `var/`, preserving clean notebook sources.

**First delivery to build:** source/config documentation, reusable dataset helpers and Notebook 00. **Second delivery:** fixed-model training/evaluation and artifact export. Neither requires modifying execution or running strategy profitability experiments first.

## 8. References and inspected code

- [Current probability contract](../../src/tyrex_pm/strategies/settlement_value/economics.py), [driver](../../src/tyrex_pm/strategies/settlement_value/driver.py), [configuration](../../src/tyrex_pm/strategies/settlement_value/config.py), [plugin](../../src/tyrex_pm/strategies/settlement_value/plugin.py).
- [Shared recording fields](../../src/tyrex_pm/strategies/settlement_value/record_schema.py), [strategy replay](../../src/tyrex_pm/research/strategy_replay/run.py), [capture import/reconstruction](../../src/tyrex_pm/research/strategy_replay/capture_replay.py), [feed and stream identities](../../src/tyrex_pm/research/strategy_replay/feed.py), [proxy labels](../../src/tyrex_pm/research/strategy_replay/labels.py).
- [Tutorial dataset patterns](../../src/tyrex_pm/research/tuto_btc5m/advanced.py), [used-holdout qualification workflow](../../src/tyrex_pm/research/strategy_lab/qualification.py), [frozen hold baseline](../../config/research/settlement_value_hold_baseline_v2.json).
- [Warehouse completion](../implementation/pm_data_warhouse/completion_status.md), [operator validation](../implementation/pm_data_warhouse/operator_validation.md), [20 October trading status](../implementation/strategies/trading_integrity_and_managed_exit_status_2026-10-20.md).
- Scikit-learn primary guidance consulted 20 October 2026: [leakage and consistent preprocessing](https://scikit-learn.org/stable/common_pitfalls.html), [probability calibration](https://scikit-learn.org/stable/modules/calibration.html), [model persistence and version compatibility](https://scikit-learn.org/stable/model_persistence.html). These inform the proposed pipeline and artifact handling; they do not qualify our data or a trading strategy.
