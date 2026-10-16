"""S2 experiment definitions (settlement_value) and a command-line runner.

    python -m tyrex_pm.research.strategy_lab.s2_experiments split
    python -m tyrex_pm.research.strategy_lab.s2_experiments vol    [--workers N]
    python -m tyrex_pm.research.strategy_lab.s2_experiments grid   [--workers N]
    python -m tyrex_pm.research.strategy_lab.s2_experiments buffer [--workers N]
    python -m tyrex_pm.research.strategy_lab.s2_experiments holdout --configs shortlist.json

Every run uses the real plugin driver through ``batch.run_batch``. Development
experiments only ever see development windows; ``holdout`` evaluates a fixed shortlist
once and refuses to run without one. Bundles land in ``var/research/strategy_runs/s2``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .batch import RunSpec, all_collections, chronological_split, run_batch, window_inventory
from .experiments import REPO, grid

OUT = REPO / "var" / "research" / "strategy_runs" / "s2"
HOLDOUT_FRACTION = 0.3
# Windows inspected while developing the plugin and runner (M1 notebook, the sigma-floor
# finding). They informed choices, so they can never be holdout evidence.
INSPECTED_DURING_DEVELOPMENT = frozenset(
    {"polymarket:condition:0x658335836e89f7c2bc7fb0d801dfffabe2b06532718afcc3418c541bfb87f241"}
)

VOL_AXES = {
    "volatility.sigma_floor": [1e-5],
    "volatility.half_life_s": [15.0, 30.0, 60.0],
    "volatility.sigma_scale": [0.75, 1.0, 1.25],
}
# The previous seed, for comparison at the default half-life and scale.
VOL_EXTRA = [{"volatility": {"sigma_floor": 1e-4, "half_life_s": 30.0, "sigma_scale": 1.0}}]
GRID_AXES = {
    "model.backend": ["endpoint_diffusion", "twap_proxy_diffusion"],
    "entry.min_edge_per_share": ["0.02", "0.05", "0.08"],
    "entry.tau_max_s": [240.0, 150.0],
    "sizing.max_debit_usdc": ["2", "5"],
}
BUFFER_TICKS = [0, 1, 2, 3]
BUFFER_SCENARIOS: list[tuple[str, dict]] = [
    ("favorable", {}),
    ("friction", {}),
    ("friction", {"order_delay_ms": 1000, "feed_delay_ms": 250}),
]


def split() -> dict:
    inventory = window_inventory()
    result = chronological_split(inventory, holdout_fraction=HOLDOUT_FRACTION)
    moved = [m for m in result["holdout"] if m in INSPECTED_DURING_DEVELOPMENT]
    result["holdout"] = [m for m in result["holdout"] if m not in INSPECTED_DURING_DEVELOPMENT]
    result["development"] = result["development"] + moved
    result["moved_to_development_as_inspected"] = moved
    result["inventory"] = {
        "windows": int(len(inventory)),
        "duplicate_rejected": int(inventory["duplicate_rejected"].sum()),
        "without_resolution": int((~inventory["has_resolution"]).sum()),
        "by_collection": inventory.groupby("collection").size().to_dict(),
    }
    return result


def _collection_of() -> dict[str, Any]:
    inventory = window_inventory()
    cols = {c.collection: c for c in all_collections()}
    return {row.market_id: cols[row.collection] for row in inventory.itertuples()}


def specs(markets: list[str], configs: list[dict], scenarios, tag: str) -> list[RunSpec]:
    owner = _collection_of()
    out = []
    for market in markets:
        for params in configs:
            for scenario, overrides in scenarios:
                out.append(
                    RunSpec(
                        collection=owner[market],
                        market_id=market,
                        parameters=params,
                        scenario=scenario,
                        scenario_overrides=overrides,
                        tag=tag,
                    )
                )
    return out


def run(name: str, *, workers: int | None = 6, shortlist: Path | None = None) -> Path:
    plan = split()
    dev, hold = plan["development"], plan["holdout"]
    out = OUT / name
    if name == "split":
        out.mkdir(parents=True, exist_ok=True)
        (out / "split.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
        return out
    if name == "vol":
        run_specs = specs(dev, grid(VOL_AXES) + VOL_EXTRA, [("favorable", {})], "vol")
        keep = True
    elif name == "grid":
        run_specs = specs(dev, grid(GRID_AXES), [("favorable", {}), ("friction", {})], "grid")
        keep = False
    elif name == "buffer":
        configs = [
            {"entry": {"limit_buffer_ticks": ticks}, "model": {"backend": "twap_proxy_diffusion"}}
            for ticks in BUFFER_TICKS
        ]
        run_specs = specs(dev, configs, BUFFER_SCENARIOS, "buffer")
        keep = False
    elif name == "holdout":
        if shortlist is None or not shortlist.exists():
            raise SystemExit("holdout requires --configs <shortlist.json> fixed in advance")
        configs = json.loads(shortlist.read_text(encoding="utf-8"))
        if not isinstance(configs, list) or not 1 <= len(configs) <= 5:
            raise SystemExit("shortlist must be a JSON list of 1-5 parameter mappings")
        run_specs = specs(hold, configs, [("favorable", {}), ("friction", {})], "holdout")
        keep = True
    else:
        raise SystemExit(f"unknown experiment {name}")
    run_batch(run_specs, out=out, workers=workers, keep_decisions=keep, split=plan)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", choices=["split", "vol", "grid", "buffer", "holdout"])
    # ~2.5 GB per worker (one prepared window in memory); 6 fits a 32 GB machine.
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--configs", type=Path, default=None)
    args = parser.parse_args(argv)
    path = run(args.experiment, workers=args.workers, shortlist=args.configs)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
