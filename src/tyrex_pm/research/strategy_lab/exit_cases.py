"""Fixed-entry exit comparisons through the shared batch and explorer contracts.

Numbers are explicit hypotheses, never a qualified live profile. Nothing runs on import.
"""
from __future__ import annotations

import hashlib
import json

from .batch import Collection, RunSpec, run_batch
from .experiments import REPO, merge
from .extreme_cases import RESEARCH_CASES
from .v3 import frozen_windows, load_candidates

ROOT = REPO / "var/research/exit_cases"
POLICY = {"execution_contract": "replay_execution_contract_v2",
          "reference_availability": "reference_availability_v1",
          "book_reconstruction": "venue_top_v1"}
VARIANTS = {
    "E0_hold": {},
    "E1_value": {"exit": {"policy": "managed_exit", "value_exit_enabled": True,
                          "value_exit_basis": "point", "value_exit_margin_per_share": "0.02",
                          "exit_cutoff_tau_s": 30.0}},
    "E2_value_trend": {"exit": {
        "policy": "managed_exit", "value_exit_enabled": True, "value_exit_basis": "point",
        "value_exit_margin_per_share": "0.02", "exit_cutoff_tau_s": 30.0,
        "value_exit_trend_enabled": True, "value_exit_trend_lookback_s": 5.0,
        "value_exit_trend_min_drop": "0.02", "value_exit_trend_confirmation_s": 2.0,
        "value_exit_trend_max_gap_s": 2.0}},
}
CASE_PURPOSES = {**RESEARCH_CASES,
    "btc-updown-5m-1792087500": "largest comparison/legacy development profit; cheap winner"}


def specification(window_ids=None, *, scenarios=("friction", "favorable")):
    windows = frozen_windows().set_index("slug")
    ids = list(CASE_PURPOSES if window_ids is None else window_ids)
    candidate = load_candidates()["comparison"]
    specs = []
    for slug in ids:
        w = windows.loc[slug]  # development-only allowlist; holdout cannot enter here
        for scenario in scenarios:
            for name, overlay in VARIANTS.items():
                specs.append(RunSpec(
                    collection=Collection(str(w.warehouse), str(w.collection)),
                    market_id=str(w.market_id), parameters=merge(candidate.parameters, overlay),
                    scenario=scenario, tag=f"{slug}--comparison--{name}",
                    execution_policy=POLICY,
                    recording={"level": "explain", "explain_trigger": "all"}))
    identity = {"kind": "exit_cases", "windows": ids, "variants": VARIANTS,
                "case_purposes": {slug: CASE_PURPOSES.get(slug, "explicit development case")
                                  for slug in ids},
                "scenarios": list(scenarios), "execution_policy": POLICY,
                "candidate": candidate.parameters}
    # Include source identity so modified implementations never reuse another's results.
    from .staged import code_identity

    identity["code"] = code_identity()
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
    return specs, ROOT / f"exits-{digest[:12]}", identity


def run(*, workers=2, window_ids=None):
    specs, out, identity = specification(window_ids)
    out.mkdir(parents=True, exist_ok=True)
    if all((out / f).exists() for f in ("summaries.parquet", "decisions.parquet", "manifest.json")):
        return out
    (out / "experiment.json").write_text(json.dumps(identity, indent=2, default=str), "utf-8")
    # One standard batch; each window loads once for all variants and scenarios.
    # Scenario controls in the explorer keep their results separate.
    run_batch(specs, out=out, workers=workers, keep_decisions=True)
    return out
