"""What each recording contains: windows, windows kept under the current policy, book loss
over time, and when continuous book gaps began. Read-only; built on the inventory index."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .inventory import capture_blocks, discover
from .recipes import RESEARCH_V2_1, _requirement_reason, book_profile

DEGRADED_HOLE_S = 5.0
DEGRADED_SHARE = 0.8
DEGRADED_MIN_RUN = 6


def _degraded_from(series: list[dict]) -> int | None:
    """First window from which at least 80% of the remaining windows have a hole ≥ 5 s."""
    flags = [(item["longest_hole_s"] or 0) >= DEGRADED_HOLE_S for item in series]
    for index in range(len(flags)):
        tail = flags[index:]
        if (
            len(tail) >= DEGRADED_MIN_RUN
            and flags[index]
            and sum(tail) / len(tail) >= DEGRADED_SHARE
        ):
            return index
    return None


def library_recordings(request: dict, root: Path) -> dict[str, Any]:
    del root
    found = discover({**request, "limit": 100000})
    roots = [Path(item) for item in request.get("roots") or []]
    blocks = {block["block_id"]: block for block in capture_blocks(roots)}
    requirements = {"coverage": "research_v2_1"}
    by_block: dict[str, list[dict]] = {}
    for row in found["windows"]:
        if row.get("block_id"):
            by_block.setdefault(row["block_id"], []).append(row)
    recordings = []
    for block_id, rows in by_block.items():
        rows.sort(key=lambda row: row.get("window_start_ns") or 0)
        series = []
        for row in rows:
            profile = book_profile(row, RESEARCH_V2_1) or {}
            reason = _requirement_reason(row, requirements, "eligible_windows")
            series.append(
                {
                    "market_id": row["market_id"],
                    "window_start_ns": row.get("window_start_ns"),
                    "valid_ratio": profile.get("valid_ratio"),
                    "longest_hole_s": profile.get("longest_hole_s"),
                    "holes": profile.get("holes") or [],
                    "kept": reason is None,
                    "reason": reason,
                    "outcome": row.get("outcome"),
                    "cut": bool(row.get("recording_cut_at_ns")),
                }
            )
        assessed = [item for item in series if item["valid_ratio"] is not None]
        lost = (
            sum(1 - item["valid_ratio"] for item in assessed) / len(assessed) if assessed else None
        )
        degraded = _degraded_from(series)
        block = blocks.get(block_id, {})
        recordings.append(
            {
                "block_id": block_id,
                "warehouse": block.get("warehouse"),
                "collection": block.get("collection"),
                "started_at_ns": block.get("started_at_ns"),
                "stopped_at_ns": block.get("stopped_at_ns"),
                "windows_until_ns": block.get("windows_until_ns"),
                "outcome_wait": block.get("outcome_wait"),
                # Complete windows whose winner was not observed; a fetch can still find it.
                "winners_missing": sum(
                    1
                    for item in series
                    if not item["cut"] and item["outcome"] == "unresolved"
                ),
                "windows": len(series),
                "kept": sum(1 for item in series if item["kept"]),
                "policy": RESEARCH_V2_1["name"],
                "book_time_lost": lost,
                "degraded_from_ns": series[degraded]["window_start_ns"]
                if degraded is not None
                else None,
                "series": series,
            }
        )
    recordings.sort(key=lambda item: item.get("started_at_ns") or 0)
    return {"recordings": recordings}
