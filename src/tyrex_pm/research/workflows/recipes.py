"""Data recipes, custom window-set filters, and preview-bound saves."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tyrex_pm.data_warehouse.contracts import file_digest
from tyrex_pm.data_warehouse.query.reader import Reader
from tyrex_pm.research.strategy_replay.loader import WindowCatalog

from .artifacts import now_iso, plan_digest, read, write_json
from .contracts import WorkflowError
from .data import selection_resolve
from .inventory import discover

SUPPORTED_ZONES = ("UTC", "Europe/Paris", "America/New_York")
UNSUPPORTED = {
    "reversal": "reversal series, threshold, amplitude, and gap handling are not defined",
    "volatility": "no versioned volatility measurement is available for window selection",
    "spread": "no versioned spread measurement is available for window selection",
    "liquidity": "no versioned liquidity measurement is available for window selection",
}
# "cookbook" resolves a recipe file (see cookbook.py) into ordinary filters.
RECIPES = ("eligible_windows", "custom_window_set", "missing_inputs", "cookbook")


def data_recipe_list(request: dict, root: Path) -> dict[str, Any]:
    del request, root
    catalog = [
        {
            "recipe": "eligible_windows",
            "applicable": True,
            "reason": "books, reference, and warm-up must be present",
        },
        {
            "recipe": "custom_window_set",
            "applicable": True,
            "reason": "filters combine with AND; picks cannot bypass guards",
        },
        {
            "recipe": "missing_inputs",
            "applicable": True,
            "reason": "diagnostic only; ordinary replay binding is refused",
        },
    ]
    for name, reason in UNSUPPORTED.items():
        catalog.append(
            {
                "recipe": name,
                "applicable": False,
                "reason": reason,
                "code": "unsupported_measurement",
            }
        )
    return {
        "schema": "workflow_data_recipe_v1",
        "recipes": catalog,
        "time_zones": list(SUPPORTED_ZONES),
    }


def _minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    total = int(hour) * 60 + int(minute)
    if not 0 <= total < 24 * 60:
        raise ValueError(value)
    return total


def zoned_start(start_ns: int, zone: str) -> tuple[int, int]:
    if zone not in SUPPORTED_ZONES:
        raise WorkflowError(
            "unsupported_timezone", "time zone is not supported", {"time_zone": zone}
        )
    try:
        tz = ZoneInfo(zone)
    except ZoneInfoNotFoundError as exc:
        raise WorkflowError(
            "unsupported_timezone", "time zone is not supported", {"time_zone": zone}
        ) from exc
    instant = datetime.fromtimestamp(start_ns / 1_000_000_000, tz=timezone.utc).astimezone(tz)
    return instant.hour * 60 + instant.minute, instant.isoweekday()


def clock_matches(start_ns: int, filters: dict) -> bool:
    daily = filters.get("daily_time") or {}
    zone = str(filters.get("time_zone") or daily.get("time_zone") or "UTC")
    minutes, weekday = zoned_start(start_ns, zone)
    weekdays = filters.get("weekdays")
    if weekdays is not None and int(weekday) not in {int(item) for item in weekdays}:
        return False
    if not daily or daily.get("enabled") is False:
        return True
    start = _minutes(str(daily["start"]))
    end = _minutes(str(daily["end"]))
    if start == end:
        raise WorkflowError(
            "invalid_filter", "daily start and end must differ", {"field": "daily_time"}
        )
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def _validate_filters(filters: dict) -> dict[str, Any]:
    filters = dict(filters or {})
    for name in filters.get("measurements") or []:
        if name in UNSUPPORTED:
            raise WorkflowError("unsupported_measurement", UNSUPPORTED[name], {"measurement": name})
    outcome = str(filters.get("outcome") or "any")
    label = str(filters.get("label_source") or "any")
    if outcome not in {"any", "up", "down", "unresolved", "unknown"}:
        raise WorkflowError(
            "invalid_filter", "outcome filter is not supported", {"outcome": outcome}
        )
    if label not in {"any", "provider_confirmed", "proxy", "unknown"}:
        raise WorkflowError(
            "invalid_filter", "label source filter is not supported", {"label_source": label}
        )
    weekdays = filters.get("weekdays")
    if weekdays is not None:
        if not list(weekdays):
            raise WorkflowError("invalid_filter", "weekday set is empty", {"field": "weekdays"})
        if any(int(day) < 1 or int(day) > 7 for day in weekdays):
            raise WorkflowError("invalid_filter", "weekdays are ISO numbers 1 through 7", {})
    daily = filters.get("daily_time") or {}
    zone = str(filters.get("time_zone") or daily.get("time_zone") or "UTC")
    if zone not in SUPPORTED_ZONES:
        raise WorkflowError(
            "unsupported_timezone", "time zone is not supported", {"time_zone": zone}
        )
    if daily.get("enabled"):
        try:
            start = _minutes(str(daily.get("start")))
            end = _minutes(str(daily.get("end")))
        except (TypeError, ValueError) as exc:
            raise WorkflowError("invalid_filter", "daily times use HH:mm", {}) from exc
        if start == end:
            raise WorkflowError(
                "invalid_filter", "daily start and end must differ", {"field": "daily_time"}
            )
    mode = str(filters.get("selection_mode") or "all_matches")
    if mode not in {"all_matches", "picked"}:
        raise WorkflowError(
            "invalid_filter", "selection mode is not supported", {"selection_mode": mode}
        )
    return {
        "outcome": outcome,
        "label_source": label,
        "daily_time": {
            "enabled": bool(daily.get("enabled")),
            "start": daily.get("start") or "00:00",
            "end": daily.get("end") or "00:00",
            "time_zone": zone,
        },
        "weekdays": None if weekdays is None else [int(day) for day in weekdays],
        "time_zone": zone,
        "selection_mode": mode,
        "picked": [str(item) for item in filters.get("picked") or []],
        # Market-condition ranges, e.g. {"realised_vol_bp": [10, 40]}; see features.py.
        "conditions": {
            str(key): list(value)
            for key, value in (filters.get("conditions") or {}).items()
            if isinstance(value, (list, tuple)) and len(value) == 2
        },
    }


def _filter_reason(row: dict, filters: dict) -> str | None:
    if row.get("partition") == "reserved" or row.get("outcome") == "withheld":
        return "reserved_evaluation"
    if row.get("inferred") or not row.get("market_id"):
        return "unrecorded_slot"
    if not clock_matches(int(row["window_start_ns"]), filters):
        return "outside_daily_time"
    if filters["outcome"] != "any" and row.get("outcome") != filters["outcome"]:
        return "outcome_mismatch"
    if filters["label_source"] != "any" and row.get("label_source") != filters["label_source"]:
        return "label_source_mismatch"
    return None


EXPLORATORY_LIMITATION = (
    "Exploratory result: provisional K accepted for replay; provider-reported winner; "
    "assumed $1/$0 payout; partial book coverage. Stored priceToBeat is recorded from "
    "provider metadata when present and is not used to decide."
)
COVERAGE_FLOORS = (0.90, 0.95, 0.99)

# replay_research_v2 (decided 23 Oct 2026, Docs/implementation/v_1_goal/
# replay_data_policy_review_2026-10-23.md). Fixed and versioned: a different rule is a
# different policy name, never a tweak of this one.
RESEARCH_V2 = {
    "name": "replay_research_v2",
    "min_valid_ratio": 0.90,
    "max_hole_s": 30.0,
    "final_protected_s": 60.0,
    "blip_ms": 250.0,
    "reference_in_window_ratio": 0.99,
}
# replay_research_v2_1 (decided 23 Oct 2026): the 30 s cap never binds in a 5-minute
# window (10% of 300 s), so the longest hole is capped at 20 s, at the natural break in
# the measured holes (none between 15 and 20 s; a group at 20-30 s inside the entry band).
RESEARCH_V2_1 = {**RESEARCH_V2, "name": "replay_research_v2_1", "max_hole_s": 20.0}
RESEARCH_POLICIES = {"research_v2": RESEARCH_V2, "research_v2_1": RESEARCH_V2_1}
RESEARCH_V2_LIMITATION = (
    "Research v2 result: both books valid at least 90% of the window with no hole longer "
    "than 30 s and none in the last 60 s (stretches under 250 ms are reconstruction blips); "
    "K from the exact start tick, not attested; provider-reported winner; assumed $1/$0 "
    "payout. Each window's holes are stored with the set."
)
NS = 10**9


def _holes(intervals: list, start: int, end: int, blip_ns: float) -> list[list[int]]:
    """Uncovered stretches of [start, end) given valid intervals, ignoring blips."""
    cursor, holes = start, []
    for a, b in sorted((int(x), int(y)) for x, y in intervals or []):
        if b <= cursor or a >= end:
            continue
        if a > cursor:
            holes.append([cursor, min(a, end)])
        cursor = max(cursor, b)
        if cursor >= end:
            break
    if cursor < end:
        holes.append([cursor, end])
    return [h for h in holes if h[1] - h[0] >= blip_ns]


def book_profile(row: dict, policy: dict = RESEARCH_V2) -> dict[str, Any] | None:
    """Union of Up and Down book holes over the market's own window, blips removed."""
    tracks = row.get("tracks") or {}
    start = int(row.get("window_start_ns") or 0)
    end = int(row.get("window_end_ns") or start + 300 * NS)
    if not start or end <= start:
        return None
    blip = policy["blip_ms"] * 10**6
    holes = []
    for name in ("up_book", "down_book"):
        holes.extend(_holes((tracks.get(name) or {}).get("intervals") or [], start, end, blip))
    merged: list[list[int]] = []
    for a, b in sorted(holes):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    lost = sum(b - a for a, b in merged)
    return {
        "policy": policy["name"],
        "valid_ratio": round(1 - lost / (end - start), 6),
        "longest_hole_s": round(max((b - a for a, b in merged), default=0) / NS, 3),
        "holes": [[round((a - start) / NS, 3), round((b - a) / NS, 3)] for a, b in merged],
        "final_hole": any(b > end - policy["final_protected_s"] * NS for _a, b in merged),
    }


def _research_v2_books(row: dict, policy: dict = RESEARCH_V2) -> str | None:
    tracks = row["tracks"]
    books = [tracks["up_book"], tracks["down_book"]]
    if any(track.get("state") == "unassessed" for track in books):
        return "book_unassessed"
    if any(not track.get("intervals") for track in books):
        return "book_absent"
    profile = book_profile(row, policy)
    if profile is None:
        return "book_unassessed"
    if profile["valid_ratio"] < policy["min_valid_ratio"]:
        return "book_valid_below_90"
    if profile["longest_hole_s"] > policy["max_hole_s"]:
        return f"book_hole_over_{int(policy['max_hole_s'])}s"
    if profile["final_hole"]:
        return "book_hole_in_final_60s"
    return None


def _research_v2_reference(row: dict) -> str | None:
    track = row["tracks"]["reference"]
    if track.get("state") == "unassessed":
        return "reference_unassessed"
    start = int(row.get("window_start_ns") or 0)
    end = int(row.get("window_end_ns") or start + 300 * NS)
    intervals = track.get("intervals") or []
    lost = sum(b - a for a, b in _holes(intervals, start, end, 0))
    ratio = 1 - lost / max(1, end - start)
    if not intervals or ratio < RESEARCH_V2["reference_in_window_ratio"]:
        return "reference_missing_in_window"
    return None


def _coverage_floor(requirements: dict, policy: str) -> float | None:
    if policy != "exploratory":
        return None
    try:
        floor = round(float(requirements.get("coverage_floor", 0.95)), 2)
    except (TypeError, ValueError) as exc:
        raise WorkflowError(
            "invalid_coverage",
            "coverage_floor is 0.90, 0.95, or 0.99",
            {"coverage_floor": requirements.get("coverage_floor")},
        ) from exc
    if floor not in COVERAGE_FLOORS:
        raise WorkflowError(
            "invalid_coverage",
            "coverage_floor is 0.90, 0.95, or 0.99",
            {"coverage_floor": floor},
        )
    return floor


def _coverage_reason(tracks: list[dict], kind: str, policy: str, floor: float | None) -> str | None:
    if any(track.get("state") == "unassessed" for track in tracks):
        return f"{kind}_unassessed"
    if any(track.get("state") == "missing" and not track.get("partial") for track in tracks):
        return f"{kind}_absent"
    if policy == "exploratory":
        for track in tracks:
            ratio = _ratio(track)
            if ratio is None or ratio < float(floor):
                return f"{kind}_below_floor"
        return None
    if any(track.get("partial") for track in tracks):
        if policy == "partial":
            return None
        return f"{kind}_partial"
    if any(track.get("state") != "present" for track in tracks):
        return f"{kind}_incomplete"
    return None


def _requirement_reason(row: dict, requirements: dict, recipe: str) -> str | None:
    reason = _data_reason(row, requirements, recipe)
    # A window the recording stopped inside fails on its books or reference; name the
    # cause instead. This only relabels: it never excludes a window that would pass.
    if (
        reason
        and row.get("recording_cut_at_ns")
        and recipe != "missing_inputs"
        and reason != "window_not_started"
    ):
        return "recording_ended_before_close"
    return reason


def _data_reason(row: dict, requirements: dict, recipe: str) -> str | None:
    tracks = row["tracks"]
    policy = str(requirements.get("coverage") or "complete")
    if policy not in {"complete", "partial", "exploratory", *RESEARCH_POLICIES}:
        raise WorkflowError(
            "invalid_coverage",
            "coverage is complete, partial, exploratory, research_v2 or research_v2_1",
            {"coverage": policy},
        )
    floor = _coverage_floor(requirements, policy)
    confidence = str(
        requirements.get("settlement_confidence")
        or ("provider_reported" if policy in {"exploratory", *RESEARCH_POLICIES} else "strict")
    )
    if confidence not in {"strict", "provider_reported"}:
        raise WorkflowError(
            "invalid_settlement_confidence",
            "settlement confidence is strict or provider_reported",
            {"settlement_confidence": confidence},
        )
    if tracks["quality"].get("reason") == "window_not_started" and recipe != "missing_inputs":
        return "window_not_started"
    if recipe == "missing_inputs":
        interesting = ("normalization", "up_book", "down_book", "reference", "quality")
        if any(
            tracks[name]["state"] in {"missing", "unassessed", "unknown"} for name in interesting
        ):
            return None
        if row["warmup"]["state"] in {"missing", "unassessed", "unknown"}:
            return None
        if any(tracks[name].get("partial") for name in ("up_book", "down_book", "reference")):
            return None
        return "no_missing_input"
    if tracks["recording"]["state"] != "present":
        return "recording_missing"
    if tracks["normalization"]["state"] != "present":
        return "normalization_needed"
    if policy in RESEARCH_POLICIES:
        # Warm-up is the strategy's own readiness question; the set requires books and the
        # reference over the market's window only.
        reason = _research_v2_books(row, RESEARCH_POLICIES[policy]) or _research_v2_reference(row)
        if reason:
            return reason
    elif requirements.get("books", True):
        reason = _coverage_reason([tracks["up_book"], tracks["down_book"]], "book", policy, floor)
        if reason:
            return reason
    if policy not in RESEARCH_POLICIES and requirements.get("reference", True):
        reason = _coverage_reason([tracks["reference"]], "reference", policy, floor)
        if reason:
            return reason
    if policy not in RESEARCH_POLICIES and requirements.get("warmup", True):
        reason = _coverage_reason([row["warmup"]], "warmup", policy, floor)
        if reason:
            return reason
    if confidence == "provider_reported":
        if row.get("outcome") not in {"up", "down"} or row.get("label_source") not in {
            "proxy",
            "provider_confirmed",
        }:
            return "provider_winner_unavailable"
    elif requirements.get("scoring_outcome") and row.get("outcome") in {
        "unknown",
        "unresolved",
        "withheld",
    }:
        return "scoring_outcome_unavailable"
    if requirements.get("provider_confirmed") and row.get("label_source") != "provider_confirmed":
        return "provider_confirmed_required"
    quality = tracks["quality"]
    if quality["state"] == "unassessed":
        return "quality_unassessed"
    if policy == "complete" and quality["state"] != "present":
        if quality.get("reason") == "semantic_unsupported":
            return "quality_semantic_unsupported"
        return "quality_unassessed"
    return None


def _ratio(track: dict) -> float | None:
    required = int(track.get("required_duration_ns") or 0)
    if required <= 0:
        return None
    return int(track.get("usable_duration_ns") or 0) / required


def _exclusion(row: dict, reason: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "market_id": row.get("market_id"),
        "reason": reason,
        "window_start_ns": row.get("window_start_ns"),
        "preparation_can_help": reason.endswith("_unassessed") or reason == "normalization_needed",
    }
    tracks = row.get("tracks") or {}
    for name in ("up_book", "down_book", "reference"):
        track = tracks.get(name) or {}
        if track.get("required_duration_ns"):
            body[f"{name}_usable_ns"] = track.get("usable_duration_ns")
            body[f"{name}_required_ns"] = track.get("required_duration_ns")
    return body


def _window_profiles(
    considered: list[dict], excluded: list[dict], requirements: dict
) -> list[dict[str, Any]]:
    """Gap profile of every considered window, kept or not, for the window map."""
    policy = RESEARCH_POLICIES.get(str(requirements.get("coverage") or ""), RESEARCH_V2_1)
    reasons = {row.get("market_id"): row.get("reason") for row in excluded}
    rows = []
    for row in considered:
        profile = book_profile(row, policy)
        rows.append(
            {
                "market_id": row["market_id"],
                "window_start_ns": row.get("window_start_ns"),
                "block_id": row.get("block_id"),
                "kept": row["market_id"] not in reasons,
                "reason": reasons.get(row["market_id"]),
                "outcome": row.get("outcome"),
                "holes": (profile or {}).get("holes") or [],
                "valid_ratio": (profile or {}).get("valid_ratio"),
                "longest_hole_s": (profile or {}).get("longest_hole_s"),
            }
        )
    rows.sort(key=lambda item: item.get("window_start_ns") or 0)
    return rows


def _exclusion_groups(excluded: list[dict]) -> list[dict[str, Any]]:
    titles = {
        "unassessed": "Not assessed yet",
        "partial": "Recorded coverage is partial",
        "below_floor": "Book coverage is below the exploratory floor",
        "book_holes": "Book or reference holes exceed research v2",
        "absent": "No usable interval was recorded",
        "unsupported": "The quality verdict does not support replay",
        "not_started": "Window starts after the last captured event",
        "cut": "The recording ended before the window closed",
        "winner": "Winner not observed yet: fetch the missing winners on the recording",
        "outside": "Outside the selected filters",
        "reserved": "Reserved evaluation",
        "other": "Other exclusions",
    }
    order = (
        "unassessed",
        "partial",
        "below_floor",
        "book_holes",
        "absent",
        "unsupported",
        "not_started",
        "cut",
        "winner",
        "outside",
        "reserved",
        "other",
    )
    grouped: dict[str, list[dict]] = {key: [] for key in order}

    def kind(reason: str) -> str:
        if reason.endswith("_unassessed") or reason == "normalization_needed":
            return "unassessed"
        if reason.endswith("_partial"):
            return "partial"
        if reason.endswith("_below_floor"):
            return "below_floor"
        if reason in {
            "book_valid_below_90",
            "book_hole_over_30s",
            "book_hole_over_20s",
            "book_hole_in_final_60s",
            "reference_missing_in_window",
        }:
            return "book_holes"
        if reason.endswith("_absent") or reason == "recording_missing":
            return "absent"
        if reason == "quality_semantic_unsupported":
            return "unsupported"
        if reason == "window_not_started":
            return "not_started"
        if reason == "recording_ended_before_close":
            return "cut"
        if reason == "provider_winner_unavailable":
            return "winner"
        if reason in {
            "outcome_mismatch",
            "label_source_mismatch",
            "outside_daily_time",
            "outside_filters",
            "unrecorded_slot",
            "outside_condition_filters",
        }:
            return "outside"
        if reason == "reserved_evaluation":
            return "reserved"
        return "other"

    for row in excluded:
        grouped[kind(str(row.get("reason") or ""))].append(row)
    groups = []
    for key in order:
        rows = grouped[key]
        if not rows:
            continue
        groups.append(
            {
                "id": key,
                "title": titles[key],
                "count": len(rows),
                "preparation_can_help": key == "unassessed",
                "examples": rows[:3],
            }
        )
    return groups


def _manifests_for(row: dict) -> list[dict]:
    reader = Reader(Path(row["warehouse"]), row["collection"])
    return list(reader.manifests)


def resolve_recipe(request: dict) -> dict[str, Any]:
    recipe = request.get("recipe") or "eligible_windows"
    if recipe in UNSUPPORTED:
        raise WorkflowError("unsupported_measurement", UNSUPPORTED[recipe], {"measurement": recipe})
    if recipe not in RECIPES:
        raise WorkflowError("unknown_recipe", "recipe is not in the catalog", {"recipe": recipe})
    cookbook = None
    if recipe == "cookbook":
        from .cookbook import apply_recipe

        cookbook = apply_recipe(request.get("cookbook") or {})
        request = {**request, "filters": cookbook["filters"]}
    filters = _validate_filters(request.get("filters") or {})
    if recipe not in {"custom_window_set", "cookbook"}:
        filters["outcome"] = "any"
        filters["label_source"] = "any"
        filters["daily_time"] = {**filters["daily_time"], "enabled": False}
        filters["weekdays"] = None
        filters["selection_mode"] = "all_matches"
        filters["conditions"] = {}
    found = discover(request)
    requirements = dict(request.get("requirements") or {})
    candidates = []
    excluded = []
    for row in found["windows"]:
        reason = _filter_reason(row, filters)
        if reason == "reserved_evaluation":
            item = _exclusion(row, reason)
            item["partition"] = "reserved"
            excluded.append(item)
            continue
        if reason:
            if row.get("market_id"):
                excluded.append(_exclusion(row, reason))
            continue
        candidates.append(row)
    resolved_quantiles = {}
    if cookbook and cookbook["quantiles"]:
        # Percentiles rank the windows that pass the filters and the data policy; the
        # resulting absolute thresholds are what the set saves and applies.
        from .cookbook import resolve_quantiles
        from .features import window_features

        ranked = [
            row for row in candidates if _requirement_reason(row, requirements, recipe) is None
        ]
        conditions, resolved_quantiles = resolve_quantiles(
            cookbook["quantiles"], window_features(ranked)
        )
        filters["conditions"] = {**filters["conditions"], **conditions}
    if filters.get("conditions"):
        from .features import feature_reason, window_features

        features = window_features(candidates)
        kept = []
        for row in candidates:
            reason = feature_reason(features.get(row["market_id"]) or {}, filters["conditions"])
            if reason:
                excluded.append(_exclusion(row, reason))
            else:
                kept.append(row)
        candidates = kept
    picked = set(filters["picked"])
    considered = candidates
    if filters["selection_mode"] == "picked":
        considered = [row for row in candidates if row["market_id"] in picked]
        reserved_ids = {
            row.get("market_id") for row in excluded if row.get("reason") == "reserved_evaluation"
        }
        for market_id in sorted(picked - {row["market_id"] for row in candidates}):
            if market_id in reserved_ids:
                continue
            excluded.append({"market_id": market_id, "reason": "outside_filters"})
    mix = {"up": 0, "down": 0, "unresolved": 0, "unknown": 0}
    for row in candidates:
        if row.get("outcome") in mix:
            mix[row["outcome"]] += 1
    included_rows = []
    for row in considered:
        reason = _requirement_reason(row, requirements, recipe)
        if reason:
            excluded.append(_exclusion(row, reason))
            continue
        included_rows.append(row)
    choice_input = [
        {
            "market_id": row["market_id"],
            "collection": row["collection"],
            "warehouse": row["warehouse"],
            "reference_series": row.get("reference_series"),
            "source_kind": "collection_recording",
        }
        for row in included_rows
    ]
    chosen = selection_resolve(
        {
            "windows": choice_input,
            "choices": request.get("choices") or {},
            "reference_series_policy": request.get("reference_series_policy") or {},
            "capture_choices": request.get("capture_choices") or {},
        },
        Path("."),
    )
    included = []
    for row in chosen["included"]:
        source = next(
            item
            for item in included_rows
            if item["market_id"] == row["market_id"] and item["collection"] == row["collection"]
        )
        included.append(
            {
                "market_id": source["market_id"],
                "collection": source["collection"],
                "warehouse": source["warehouse"],
                "reference_series": source.get("reference_series"),
                "window_start_ns": source["window_start_ns"],
                "window_end_ns": source["window_end_ns"],
                "outcome": source.get("outcome"),
                "label_source": source.get("label_source"),
                "partition": source.get("partition"),
                "up_book_usable_ratio": _ratio(source["tracks"]["up_book"]),
                "down_book_usable_ratio": _ratio(source["tracks"]["down_book"]),
                "reference_usable_ratio": _ratio(source["tracks"]["reference"]),
                "coverage": requirements.get("coverage") or "complete",
                "coverage_floor": requirements.get("coverage_floor"),
                "settlement_confidence": requirements.get("settlement_confidence"),
                "block_id": source.get("block_id"),
                "k_source": "reconstructed_reference",
                "price_to_beat_recorded": bool(source.get("price_to_beat_recorded")),
                "price_to_beat_value": source.get("price_to_beat_value"),
                "price_to_beat_status": source.get("price_to_beat_status") or "unavailable",
                "price_to_beat_source": source.get("price_to_beat_source"),
                "price_to_beat_comparison": "not_compared",
                **(
                    {
                        "book_profile": book_profile(
                            source, RESEARCH_POLICIES[str(requirements.get("coverage"))]
                        )
                    }
                    if requirements.get("coverage") in RESEARCH_POLICIES
                    else {}
                ),
            }
        )
    excluded.extend(chosen["excluded"])
    conditioned = filters["outcome"] != "any" or filters["label_source"] != "any"
    population = "outcome_conditioned" if conditioned else "unconditioned"
    intended = (
        "diagnostic"
        if conditioned or recipe == "missing_inputs"
        else request.get("intended_use") or "development"
    )
    if recipe == "missing_inputs":
        intended = "diagnostic"
    coverage = str(requirements.get("coverage") or "")
    exploratory = coverage in {"exploratory", *RESEARCH_POLICIES}
    if exploratory and recipe != "missing_inputs":
        intended = "exploratory_research"
    if coverage in RESEARCH_POLICIES:
        policy_used = RESEARCH_POLICIES[coverage]
        research_policy = policy_used["name"]
        research_limitation = RESEARCH_V2_LIMITATION.replace(
            "longer than 30 s", f"longer than {int(policy_used['max_hole_s'])} s"
        )
    elif exploratory:
        research_policy, research_limitation = "exploratory", EXPLORATORY_LIMITATION
    else:
        research_policy, research_limitation = None, None
    cookbook_record = (
        {**cookbook["record"], "resolved_quantiles": resolved_quantiles} if cookbook else None
    )
    preview_body = {
        "recipe": recipe,
        "recipe_version": "workflow_data_recipe_v1",
        "cookbook": cookbook_record,
        "filters": filters,
        "scope": {
            "roots": request.get("roots"),
            "collection": request.get("collection"),
            "start_utc": request.get("start_utc"),
            "end_utc": request.get("end_utc"),
            "asset": request.get("asset"),
            "duration": request.get("duration"),
            "block_ids": request.get("block_ids") or [],
        },
        "requirements": requirements,
        "choices": request.get("choices") or {},
        "reference_series_policy": request.get("reference_series_policy") or {},
        "inventory_fingerprint": found["inventory_fingerprint"],
        "included": [row["market_id"] for row in included],
        "excluded": [(row.get("market_id"), row.get("reason")) for row in excluded],
    }
    return {
        "schema": "workflow_selection_preview_v1",
        "recipe": recipe,
        "cookbook": cookbook_record,
        "filters": filters,
        "candidates": [
            {
                "market_id": row["market_id"],
                "collection": row["collection"],
                "window_start_ns": row["window_start_ns"],
                "outcome": row.get("outcome"),
                "label_source": row.get("label_source"),
                "availability": row.get("availability"),
                "partition": row.get("partition"),
            }
            for row in candidates
        ],
        "included": included,
        "excluded": excluded,
        "exclusion_groups": _exclusion_groups(excluded),
        "window_profiles": _window_profiles(considered, excluded, requirements),
        "outcome_mix": mix,
        "outcome_mix_omits_withheld": True,
        "requires_choice": chosen["requires_choice"],
        "preparation_needed": [
            row["market_id"]
            for row in candidates
            if row["tracks"]["normalization"]["state"] != "present"
            or row["tracks"]["quality"]["state"] == "unassessed"
        ],
        "population": population,
        "intended_use": intended,
        "preview_id": plan_digest(preview_body),
        "inventory_fingerprint": found["inventory_fingerprint"],
        "publication_ids": found["publication_ids"],
        "assessed_at": found["assessed_at"],
        "supported_filter": True,
        "matched": len(candidates),
        "research_policy": research_policy,
        "policy_parameters": RESEARCH_POLICIES.get(coverage),
        "research_limitation": research_limitation,
        "coverage_floor": requirements.get("coverage_floor"),
        "settlement_confidence": requirements.get("settlement_confidence"),
        "block_ids": request.get("block_ids") or [],
        "clock": {
            "time_zone": filters["time_zone"],
            "weekday_basis": "zoned_window_start",
            "overnight": "late_interval_or_early_interval_on_the_start_day",
            "date_bounds": "utc_half_open_window_start",
        },
    }


def data_recipe_resolve(request: dict, root: Path) -> dict[str, Any]:
    from .sets import reserved_overlap

    result = resolve_recipe(request)
    result["reserved_overlap"] = reserved_overlap(
        root, {row["market_id"] for row in result["included"]}
    )
    return result


_DIGESTS: dict[tuple[str, int, int], str] = {}
# Verification receipts survive a restart. A digest is reused only while the file keeps
# the same size and modification time; any change re-hashes it.
_DIGESTS_LOADED: set[Path] = set()
_DIGESTS_DIRTY: set[Path] = set()


def _cache_dir(saved: dict) -> Path:
    """Receipts live next to the warehouses: var/warehouse_* -> var/research/cache."""
    rows = saved.get("included") or [{}]
    warehouse = Path(str(rows[0].get("warehouse") or saved.get("warehouse") or "."))
    return warehouse.resolve().parent / "research" / "cache"


def _load_digests(cache: Path) -> None:
    if cache in _DIGESTS_LOADED:
        return
    _DIGESTS_LOADED.add(cache)
    path = cache / "pin_digests.json"
    if path.is_file():
        try:
            for key, value in read(path).items():
                path, size, mtime = key.rsplit("|", 2)
                _DIGESTS[(path, int(size), int(mtime))] = value
        except (OSError, ValueError):
            _DIGESTS.clear()


def _save_digests(cache: Path) -> None:
    if cache not in _DIGESTS_DIRTY:
        return
    cache.mkdir(parents=True, exist_ok=True)
    write_json(
        cache / "pin_digests.json", {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in _DIGESTS.items()}
    )
    _DIGESTS_DIRTY.discard(cache)


def _file_pins(manifests: list[dict], warehouse: Path) -> list[dict]:
    """Record the checksum the build already stored. Bytes are checked on open and bind."""
    pins = []
    for build in manifests:
        for item in build.get("files") or []:
            path = warehouse / item["path"]
            expected = item.get("sha256")
            if not path.is_file() or not isinstance(expected, str) or len(expected) != 64:
                raise WorkflowError(
                    "pin_mismatch",
                    "manifest checksum does not match the file that would be pinned",
                    {"path": str(path)},
                )
            stat = path.stat()
            pins.append(
                {
                    "path": str(path.resolve()),
                    "sha256": expected,
                    "dataset": item.get("dataset"),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "checksum_source": "manifest",
                }
            )
    return pins


def _confirmed_digest(path: Path, expected: str) -> None:
    if not path.is_file():
        raise WorkflowError(
            "pin_mismatch",
            "pinned bytes changed; latest data was not substituted",
            {"path": str(path)},
        )
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    cached = _DIGESTS.get(key)
    if cached == expected:
        return
    digest = file_digest(path)
    if digest != expected:
        raise WorkflowError(
            "pin_mismatch",
            "pinned bytes changed; latest data was not substituted",
            {"path": str(path)},
        )
    _DIGESTS[key] = digest
    _DIGESTS_DIRTY.update(_DIGESTS_LOADED)


def save_from_preview(request: dict, root: Path) -> dict[str, Any]:
    preview_id = request.get("preview_id")
    if not preview_id:
        raise WorkflowError("missing_field", "preview_id is required", {"field": "preview_id"})
    preview = resolve_recipe(request)
    if preview["preview_id"] != preview_id:
        raise WorkflowError(
            "preview_stale",
            "the warehouse or the filters changed after the reviewed preview",
            {"preview_id": preview_id, "current": preview["preview_id"]},
        )
    if preview["requires_choice"]:
        raise WorkflowError(
            "ambiguous_duplicate",
            "an explicit source choice is required before this selection can be saved",
            {"market_ids": preview["requires_choice"]},
        )
    if not preview["included"]:
        raise WorkflowError("empty_selection", "the reviewed preview includes no windows", {})
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in preview["included"]:
        grouped.setdefault((row["warehouse"], row["collection"]), []).append(row)
    manifests = []
    file_pins = []
    for (warehouse, collection), rows in grouped.items():
        builds = _manifests_for(rows[0])
        manifests.extend(builds)
        file_pins.extend(_file_pins(builds, Path(warehouse)))
    conditioned = preview["population"] == "outcome_conditioned"
    body = {
        "schema": "workflow_selection_v1",
        "selection_id": request.get("selection_id") or uuid.uuid4().hex,
        "name": request.get("name") or "",
        "intended_use": preview["intended_use"],
        "population": preview["population"],
        "outcome_conditioned": conditioned,
        "created_at": now_iso(),
        "recipe": preview["recipe"],
        "cookbook": preview.get("cookbook"),
        "filters": preview["filters"],
        "requirements": request.get("requirements") or {},
        "included": preview["included"],
        "excluded": preview["excluded"],
        "reference_series_policy": request.get("reference_series_policy") or {},
        "capture_provenance": request.get("capture_provenance") or [],
        "pin_parts": file_pins,
        "manifests": manifests,
        "warehouse": preview["included"][0]["warehouse"],
        "collection": preview["included"][0]["collection"],
        "preview_id": preview["preview_id"],
        "inventory_fingerprint": preview["inventory_fingerprint"],
        "legacy_unpinned": False,
        "block_ids": request.get("block_ids") or preview.get("block_ids") or [],
        "research_policy": preview.get("research_policy"),
        "policy_parameters": preview.get("policy_parameters"),
        "research_limitation": preview.get("research_limitation"),
        "coverage_floor": preview.get("coverage_floor"),
        "settlement_confidence": preview.get("settlement_confidence"),
        "start_utc": request.get("start_utc") or "",
        "end_utc": request.get("end_utc") or "",
    }
    body["digest"] = plan_digest(
        {
            "included": body["included"],
            "manifests": body["manifests"],
            "pin_parts": body["pin_parts"],
            "filters": body["filters"],
            "recipe": body["recipe"],
            "preview_id": body["preview_id"],
        }
    )
    path = root / "selections" / f"{body['selection_id']}.json"
    if path.exists():
        raise WorkflowError(
            "selection_exists",
            "a saved selection is immutable; create a new id",
            {"selection_id": body["selection_id"]},
        )
    write_json(path, body)
    return {
        "selection_id": body["selection_id"],
        "digest": body["digest"],
        "included": len(body["included"]),
        "excluded": len(body["excluded"]),
        "preview_id": body["preview_id"],
        "filters": body["filters"],
        "population": body["population"],
        "intended_use": body["intended_use"],
        "path": str(path),
    }


def saved_selection(root: Path, selection_id: str) -> dict[str, Any]:
    path = root / "selections" / f"{selection_id}.json"
    if not path.exists():
        raise WorkflowError("not_found", "selection is not saved", {"selection_id": selection_id})
    return read(path)


def verify_saved_catalog(saved: dict) -> list[str]:
    manifests = saved.get("manifests") or []
    if _legacy(saved):
        return []
    cache = _cache_dir(saved)
    _load_digests(cache)
    for item in saved.get("pin_parts") or []:
        _confirmed_digest(Path(item["path"]), str(item.get("sha256") or ""))
    _save_digests(cache)
    if not saved.get("included"):
        return []
    # Every pinned file was just confirmed byte-for-byte. If the same pins already proved
    # this membership, the catalog read would only repeat that proof.
    receipt_key = plan_digest(
        {
            "pins": [(item["path"], item.get("sha256")) for item in saved.get("pin_parts") or []],
            "manifests": [item.get("build_id") for item in manifests],
            "included": sorted(row["market_id"] for row in saved["included"]),
        }
    )
    receipt_path = cache / "memberships" / f"{receipt_key}.json"
    if receipt_path.is_file():
        try:
            return list(read(receipt_path)["catalog"])
        except (OSError, ValueError, KeyError):
            pass
    groups: dict[tuple[str, str], list[str]] = {}
    for row in saved["included"]:
        groups.setdefault((row["warehouse"], row["collection"]), []).append(row["market_id"])
    found: list[str] = []
    for (warehouse, collection), market_ids in groups.items():
        builds = [item for item in manifests if item.get("collection_id") in {None, collection}]
        reader = Reader(Path(warehouse), collection, manifests=builds or list(manifests))
        catalog = [ref.market_id for ref in WindowCatalog(reader).windows()]
        if not catalog:
            raise WorkflowError(
                "empty_catalog",
                "the pinned catalog has no windows for this membership",
                {"selection_id": saved.get("selection_id"), "collection": collection},
            )
        missing = [market_id for market_id in market_ids if market_id not in catalog]
        if missing:
            raise WorkflowError(
                "pin_membership",
                "saved markets are not in the pinned catalog",
                {"market_ids": missing},
            )
        found.extend(catalog)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(receipt_path, {"verified_at": now_iso(), "catalog": found})
    return found


def _holdout_overlap(saved: dict) -> int:
    # Sets carry no partition; the notebook split no longer marks any window.
    reserved: set[str] = set()
    if not reserved:
        return 0
    return sum(
        1 for row in saved.get("included") or [] if str(row.get("market_id") or "") in reserved
    )


def _compatibility(saved: dict) -> str:
    if _legacy(saved):
        return "legacy_unpinned"
    if saved.get("research_policy") in {
        "exploratory",
        *(p["name"] for p in RESEARCH_POLICIES.values()),
    }:
        return str(saved["research_policy"])
    if saved.get("recipe") == "missing_inputs":
        return "not_for_replay"
    return "saved_before_exploratory_policy"


def selection_list(request: dict, root: Path) -> dict[str, Any]:
    from .trash import set_status

    folder = root / "selections"
    rows = []
    if folder.is_dir():
        wanted = request.get("selection_id")
        for path in sorted(folder.glob("*.json")):
            if wanted and path.stem != str(wanted):
                continue
            saved = read(path)
            starts = [
                int(row["window_start_ns"])
                for row in saved.get("included") or []
                if row.get("window_start_ns") is not None
            ]
            ends = [
                int(row["window_end_ns"])
                for row in saved.get("included") or []
                if row.get("window_end_ns") is not None
            ]
            rows.append(
                {
                    "selection_id": saved.get("selection_id") or path.stem,
                    "start_ns": min(starts) if starts else None,
                    "end_ns": max(ends) if ends else None,
                    "digest": saved.get("digest"),
                    "name": saved.get("name") or "",
                    "recipe": saved.get("recipe"),
                    "cookbook": {
                        key: (saved.get("cookbook") or {}).get(key)
                        for key in ("id", "version", "title", "parameters", "resolved_quantiles")
                    }
                    if saved.get("cookbook")
                    else None,
                    "included": len(saved.get("included") or []),
                    "intended_use": saved.get("intended_use"),
                    "population": saved.get("population"),
                    "outcome_conditioned": saved.get("outcome_conditioned"),
                    "research_policy": saved.get("research_policy"),
                    "research_limitation": saved.get("research_limitation"),
                    # Sets saved through the API without a scope still name each window's
                    # recording.
                    "block_ids": saved.get("block_ids")
                    or sorted(
                        {
                            row["block_id"]
                            for row in saved.get("included") or []
                            if row.get("block_id")
                        }
                    ),
                    "created_at": saved.get("created_at"),
                    "legacy_unpinned": _legacy(saved),
                    "coverage_floor": saved.get("coverage_floor"),
                    "settlement_confidence": saved.get("settlement_confidence"),
                    "holdout_overlap": _holdout_overlap(saved),
                    "compatibility": _compatibility(saved),
                    "status": set_status(saved),
                    "parent_selection_id": saved.get("parent_selection_id"),
                    "version": saved.get("version") or 1,
                    "policy_parameters": saved.get("policy_parameters"),
                }
            )
    from .artifacts import slice_page

    page, cursor = slice_page(rows, request)
    return {"selections": page, "next_cursor": cursor}


def _legacy(saved: dict) -> bool:
    manifests = saved.get("manifests") or []
    return bool(manifests) and all(
        item.get("build_id") == "workflow_pin" and not item.get("files") for item in manifests
    )


def selection_derive(request: dict, root: Path) -> dict[str, Any]:
    """Rebuild a saved set under a policy and/or with more recordings, as a linked version.

    The source set is never modified. ``save`` false returns the preview and the difference
    in windows; ``save`` true writes the new set with ``parent_selection_id`` and ``version``.
    """
    from .inventory import capture_blocks
    from .recording import warehouse_roots

    source_id = str(request.get("selection_id") or "")
    path = root / "selections" / f"{source_id}.json"
    if not path.is_file():
        raise WorkflowError("not_found", "window set is not saved", {"selection_id": source_id})
    saved = read(path)
    if saved.get("recipe") == "missing_inputs":
        raise WorkflowError("diagnostic_not_derivable", "diagnostic sets are not replay sets", {})
    coverage = str(request.get("coverage") or "research_v2_1")
    if coverage not in RESEARCH_POLICIES:
        raise WorkflowError(
            "invalid_coverage", "derive under a research policy", {"coverage": coverage}
        )
    added = [str(item) for item in request.get("add_block_ids") or []]
    # Sets saved through the API without a recording scope still name each window's recording.
    original = saved.get("block_ids") or [
        row["block_id"] for row in saved.get("included") or [] if row.get("block_id")
    ]
    block_ids = list(dict.fromkeys([*original, *added]))
    if not block_ids:
        raise WorkflowError(
            "set_without_recordings",
            "this set was saved without its recordings; create it again from the Data Library",
            {"selection_id": source_id},
        )
    blocks = {block["block_id"]: block for block in capture_blocks(warehouse_roots())}
    missing = [item for item in block_ids if item not in blocks]
    if missing:
        raise WorkflowError(
            "unknown_recording", "recording is not available", {"block_ids": missing}
        )
    scope = {
        "roots": sorted({blocks[item]["warehouse"] for item in block_ids}),
        "block_ids": block_ids,
        "recipe": saved.get("recipe") or "eligible_windows",
        "filters": saved.get("filters") or {},
        # A cookbook set re-derives from its own copy of the recipe and parameters, so a
        # percentile threshold is ranked again over the new recordings.
        "cookbook": saved.get("cookbook"),
        "requirements": {"coverage": coverage},
        "start_utc": saved.get("start_utc") or None,
        "end_utc": saved.get("end_utc") or None,
    }
    preview = resolve_recipe(scope)
    before = {row["market_id"] for row in saved.get("included") or []}
    after = {row["market_id"] for row in preview["included"]}
    difference = {
        "kept": len(before & after),
        "added": sorted(after - before),
        "removed": sorted(before - after),
        "before": len(before),
        "after": len(after),
    }
    version = int(saved.get("version") or 1) + 1
    if not request.get("save"):
        return {
            "selection_id": source_id,
            "policy": RESEARCH_POLICIES[coverage]["name"],
            "block_ids": block_ids,
            "difference": difference,
            "preview_id": preview["preview_id"],
            "version": version,
        }
    name = str(request.get("name") or "").strip() or (
        f"{saved.get('name') or 'Window set'} · v{version}"
    )
    result = save_from_preview({**scope, "preview_id": preview["preview_id"], "name": name}, root)
    new_path = root / "selections" / f"{result['selection_id']}.json"
    body = read(new_path)
    body["parent_selection_id"] = source_id
    body["version"] = version
    body["derived"] = {"coverage": coverage, "added_block_ids": added, "difference": difference}
    write_json(new_path, body)
    return {
        **result,
        "parent_selection_id": source_id,
        "version": version,
        "difference": difference,
    }
