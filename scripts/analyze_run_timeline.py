"""Quick timeline extractor for run review."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

KEY_TYPES = {
    "paired_binary_entry_submitted",
    "paired_binary_entry_skip",
    "paired_binary_activation_reference",
    "paired_binary_stop_plan",
    "paired_binary_leg_stop",
    "paired_binary_winner_target",
    "survivor_hard_floor_set",
    "survivor_hard_floor_triggered",
    "survivor_recovery_level_computed",
    "survivor_trailing_stop_armed",
    "survivor_trailing_stop_triggered",
    "survival_enforce_exit_requested",
    "survival_enforce_exit_submitted",
    "survival_enforce_exit_skipped",
    "survival_monitor_evaluated",
    "survival_exit_order_type_selected",
    "paired_binary_state_change",
    "paired_binary_done",
    "paired_binary_terminal_summary",
    "paired_binary_no_entry_summary",
    "strategy_lifecycle_pre_close_flatten_required",
    "paired_binary_open_exposure_at_shutdown",
    "oms_submit",
    "paired_binary_survivor_force_exit_started",
    "paired_binary_survivor_force_exit_done",
}


def analyze(run: str) -> None:
    path = Path("var/reporting/runs") / run / "facts.jsonl"
    facts = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    types = Counter(f["fact_type"] for f in facts)
    print("=" * 80)
    print("RUN", run, "| facts:", len(facts))
    print("Top types:")
    for t, c in types.most_common(12):
        print(f"  {c:4d}  {t}")
    print("\nTimeline:")
    for f in facts:
        ft = f["fact_type"]
        if ft not in KEY_TYPES and not any(
            x in ft
            for x in (
                "survivor",
                "survival",
                "stop_plan",
                "leg_stop",
                "winner_target",
                "activation",
                "entry_submitted",
                "state_change",
                "done",
                "terminal",
                "flatten",
                "open_exposure",
            )
        ):
            continue
        p = f.get("payload", {})
        ts = (f.get("ts") or "")[:19]
        extra = ""
        if ft == "paired_binary_state_change":
            extra = f" {p.get('from')}->{p.get('to')} reason={p.get('reason')}"
        elif "survivor" in ft or "survival" in ft:
            keys = (
                "survivor_leg",
                "floor_price",
                "breakeven_price",
                "activation_price",
                "current_executable_bid",
                "activation_mode",
                "trail_floor",
                "peak_executable_bid",
                "reason",
                "enforcement_mode",
                "trigger",
                "module",
                "state",
            )
            extra = " " + str({k: p.get(k) for k in keys if p.get(k) is not None})
        elif ft == "oms_submit":
            matched = "matched" in str(p.get("oms_result", ""))
            extra = f" matched={matched} corr={f.get('correlation_id')}"
        elif "entry" in ft:
            extra = f" reason={p.get('reason')} state={p.get('state')}"
        elif ft in {"paired_binary_done", "paired_binary_terminal_summary", "paired_binary_no_entry_summary"}:
            extra = f" final={p.get('final_state') or p.get('phase')} reason={p.get('reason')}"
        print(f"  {ts}  {ft}{extra}")
    print()


if __name__ == "__main__":
    for r in sys.argv[1:]:
        analyze(r)
