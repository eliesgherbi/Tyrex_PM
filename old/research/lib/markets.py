"""Clean-market filter and Notebook 01 coverage audit (M2B.4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from research.lib.loaders import DayPartition, table


def _bool_ptb(val: Any) -> bool:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return False
    s = str(val).strip()
    return s != "" and s.lower() != "none"


def build_clean_markets(
    day: DayPartition,
    *,
    gap_rate_feature_threshold: float = 0.98,
) -> pd.DataFrame:
    """Build clean-market audit table for Notebook 01."""
    markets = table(day, "markets")
    wsq = table(day, "ws_quality")
    if markets.empty:
        return pd.DataFrame()

    gap_map = {}
    if not wsq.empty and "market_id" in wsq.columns:
        gap_map = wsq.set_index("market_id")["gap_rate"].to_dict()

    rows: list[dict[str, Any]] = []
    for _, m in markets.iterrows():
        mid = m["market_id"]
        ptb_ok = _bool_ptb(m.get("price_to_beat"))
        final_ok = _bool_ptb(m.get("final_reference_price"))
        coverage = str(m.get("coverage_status") or "")
        dropped = int(m.get("dropped_events") or 0)
        corrupt = int(m.get("corrupt_row_count") or 0)
        gap_rate = float(gap_map.get(mid, m.get("ws_seq_gap_count", 0) / max(int(m.get("event_count") or 1), 1)))

        exclusion_reason = ""
        include = True
        if coverage not in {"recorded", "partial"}:
            include = False
            exclusion_reason = f"coverage_status={coverage}"
        elif dropped > 0:
            include = False
            exclusion_reason = f"dropped_events={dropped}"
        elif corrupt > 0:
            include = False
            exclusion_reason = f"corrupt_row_count={corrupt}"
        elif not ptb_ok:
            include = False
            exclusion_reason = "price_to_beat_missing"

        gap_usable = gap_rate < gap_rate_feature_threshold
        label_status = "valid_proxy" if ptb_ok and final_ok else ("partial" if ptb_ok else "missing_ptb")

        rows.append(
            {
                "market_id": mid,
                "event_start_ts": m.get("event_start_ts"),
                "event_end_ts": m.get("event_end_ts"),
                "include_for_analysis": include,
                "exclusion_reason": exclusion_reason,
                "ptb_present": ptb_ok,
                "final_reference_present": final_ok,
                "coverage_status": coverage,
                "dropped_events": dropped,
                "corrupt_row_count": corrupt,
                "gap_rate": round(gap_rate, 6),
                "gap_rate_usable_as_feature": gap_usable,
                "label_validity_status": label_status,
            }
        )
    return pd.DataFrame(rows)


def load_clean_markets(path: Path | str) -> pd.DataFrame:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"clean markets artifact not found: {p}")
    if p.suffix == ".json":
        return pd.read_json(p)
    return pd.read_csv(p)


def included_market_ids(clean: pd.DataFrame) -> list[str]:
    if clean.empty:
        return []
    return clean.loc[clean["include_for_analysis"] == True, "market_id"].astype(str).tolist()  # noqa: E712


def ws_seq_gap_audit(lifecycle: pd.DataFrame) -> dict[str, Any]:
    if lifecycle.empty:
        return {"total_events": 0, "ws_seq_gap_count": 0, "verdict": "no_data"}
    total = len(lifecycle)
    gaps = int((lifecycle["event_type"] == "ws_seq_gap").sum()) if "event_type" in lifecycle.columns else 0
    verdict = "diagnostic_only" if gaps / max(total, 1) > 0.5 else "mixed"
    return {
        "total_events": total,
        "ws_seq_gap_count": gaps,
        "gap_fraction": round(gaps / max(total, 1), 4),
        "verdict": verdict,
    }


def write_clean_markets(clean: pd.DataFrame, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    clean.to_csv(out_path, index=False)
    return out_path


def notebook_01_decisions(
    day: DayPartition,
    clean: pd.DataFrame,
    gap_audit: dict[str, Any],
) -> dict[str, Any]:
    n_included = int(clean["include_for_analysis"].sum()) if not clean.empty else 0
    gap_usable = bool(clean["gap_rate_usable_as_feature"].any()) if not clean.empty else False
    label_ok = int((clean["label_validity_status"] == "valid_proxy").sum()) if not clean.empty else 0
    return {
        "clean_markets_artifact": "research/output/m2b4/clean_markets.csv",
        "markets_total": len(clean),
        "markets_included": n_included,
        "label_validity_verdict": "acceptable_proxy" if label_ok >= max(1, n_included // 2) else "provisional_proxy",
        "gap_rate_usability_verdict": gap_audit.get("verdict", "unknown"),
        "gap_rate_usable_as_feature": gap_usable,
        "min_stable_sample_markets": 500,
        "current_sample_provisional": True,
    }
