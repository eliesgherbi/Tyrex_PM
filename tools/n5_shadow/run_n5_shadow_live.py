#!/usr/bin/env python3
"""N5B live-input SHADOW (public market data + simulated ShadowOMS).

``--mode live`` means live *inputs* only. Execution remains SHADOW:
ShadowOMS + shadow_depth_walk_v1. No LiveOMS. No auth. No venue mutation.
``orders_live`` is always 0.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from tyrex_pm.core.clock import SystemClock
from tyrex_pm.core.snapshots import BookSnapshot
from tyrex_pm.execution.shadow_fill_model import FILL_MODEL_DEPTH_WALK_V1
from tyrex_pm.runtime.config import load_observe_config, observe_config_from_mapping
from tyrex_pm.runtime.live_zgap_compose import (
    run_live_zgap_compose,
    seconds_until_next_boundary,
)
from tyrex_pm.runtime.n5_shadow_runtime import N5ShadowRuntime

REPO = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO / "config" / "observe_shadow_z_gap_n5b_live.json"


def _assert_no_liveoms_in_module() -> None:
    text = (REPO / "src" / "tyrex_pm" / "runtime" / "n5_shadow_runtime.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(text)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    if any("live_oms" in m for m in imports):
        raise SystemExit("REFUSE: LiveOMS import detected in n5_shadow_runtime")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="N5B live-input SHADOW (simulated execution only)"
    )
    ap.add_argument("--mode", choices=("live",), required=True)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--duration-s", type=float, default=180.0)
    ap.add_argument("--max-windows", type=int, default=2)
    ap.add_argument("--min-seals", type=int, default=None)
    ap.add_argument("--prep-lead-s", type=float, default=45.0)
    ap.add_argument("--wait-for-boundary", action="store_true")
    ap.add_argument(
        "--out",
        type=Path,
        default=REPO / "var" / "reporting" / "n5" / "shadow_live_summary.json",
    )
    args = ap.parse_args()
    _assert_no_liveoms_in_module()

    config = load_observe_config(args.config)
    if config.shadow is None or not config.shadow.enable_oms:
        raise SystemExit("N5B requires shadow.enable_oms=true")
    if config.shadow.fill_model_id != FILL_MODEL_DEPTH_WALK_V1:
        raise SystemExit(f"N5B requires fills.model_id={FILL_MODEL_DEPTH_WALK_V1}")
    if config.risk is None or config.risk.runtime_mode.value != "SHADOW":
        raise SystemExit("N5B requires risk.runtime_mode=SHADOW")

    run_id = datetime.now(timezone.utc).strftime("n5b_%Y%m%dT%H%M%SZ")
    out_dir = args.out.parent / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    if config.shadow.persistence_path is not None:
        raw = json.loads(Path(args.config).read_text(encoding="utf-8"))
        raw["output_path"] = str(out_dir / "facts.jsonl")
        raw["shadow"]["persistence_path"] = str(out_dir / "state.json")
        config = observe_config_from_mapping(raw)

    if args.wait_for_boundary:
        wait = seconds_until_next_boundary() - float(args.prep_lead_s)
        if wait > 1.0:
            print(json.dumps({"waiting_s": wait}, indent=2))
            import time

            time.sleep(wait)

    clock = SystemClock()
    n5 = N5ShadowRuntime.create(
        config,
        clock=clock,
        basis_ewma_half_life_s=30.0,  # live-validation only; production OPEN
    )
    min_seals = args.min_seals if args.min_seals is not None else max(1, args.max_windows)

    def on_book(book: BookSnapshot) -> None:
        n5.publish_book(book, available_at=clock.now_utc())

    def on_active_session(session) -> None:
        n5._activate_session(session)

    def n5_evaluate() -> list[dict]:
        return [n5.evaluate_shadow(trigger="feed").to_dict()]

    composed = asyncio.run(
        run_live_zgap_compose(
            mode="n5_shadow",
            out_dir=out_dir,
            run_id=run_id,
            min_seals=min_seals,
            max_duration_s=args.duration_s,
            prep_lead_s=args.prep_lead_s,
            runtime=n5.n4,
            on_book=on_book,
            on_active_session=on_active_session,
            n5_evaluate=n5_evaluate,
            stop_when_seals_met=True,
        )
    )
    d = composed.to_dict()
    summary = {
        "mode": "live",
        "n5b": True,
        "not_live_evidence": False,
        "tls_verify": True,
        "live_oms": False,
        "orders_live": 0,
        "venue_mutation": False,
        "auth_touched": False,
        "fill_model_id": FILL_MODEL_DEPTH_WALK_V1,
        "economics_label": "simulated_shadow",
        "fees_label": "estimated",
        "started_at": d["started_at"],
        "ended_at": d["ended_at"],
        "run_id": run_id,
        "out_dir": str(out_dir),
        "discovery": d["discovery"],
        "feeds": d["feeds"],
        "clock": d["clock"],
        "seals": d["seals"],
        "missed_windows": d["missed_windows"],
        "shadow_records": d["shadow_records"],
        "shadow_record_count": d["shadow_record_count"],
        "errors": d["errors"],
        "orders_submitted": n5.orders_submitted,
        "lifecycle": n5.host.lifecycle.state.value,
        "portfolio_flat": n5.host.portfolio.is_flat(),
        "oms_is_shadow": type(n5.host.oms).__name__ if n5.host.oms else None,
        "gate_notes": d["gate_notes"],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: summary[k]
                for k in summary
                if k not in {"shadow_records", "observations"}
            },
            indent=2,
        )[:6000]
    )
    ok = (
        summary.get("orders_live") == 0
        and summary.get("venue_mutation") is False
        and summary.get("fill_model_id") == FILL_MODEL_DEPTH_WALK_V1
        and summary.get("live_oms") is False
        and len(summary.get("seals") or []) >= 1
        and summary.get("oms_is_shadow") == "ShadowOMS"
    )
    raise SystemExit(0 if ok else 2)


if __name__ == "__main__":
    main()
