"""Read recorded JSONL archives (M2B.3)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tyrex_pm.core.events import MarketEvent, event_from_dict
from tyrex_pm.reporting.event_sink import read_event_segment_lines


@dataclass
class EventReadStats:
    corrupt_row_count: int = 0
    duplicate_event_count: int = 0
    seen_event_ids: set[str] = field(default_factory=set)


def _looks_like_day_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    if (path / "coverage_report.json").is_file() or (path / "heartbeat.json").is_file():
        return True
    return any(p.is_dir() and (p / "manifest.json").is_file() for p in path.iterdir())


def resolve_day_dir(recordings: Path, *, date: str | None = None) -> tuple[Path, str]:
    recordings = recordings.resolve()
    if date:
        day_dir = recordings if recordings.name == date else recordings / date
        if not day_dir.is_dir():
            raise FileNotFoundError(f"recording day not found: {day_dir}")
        return day_dir, date
    if _looks_like_day_dir(recordings):
        return recordings, recordings.name
    if recordings.name.startswith("20") and recordings.is_dir():
        return recordings, recordings.name
    raise ValueError("pass --date or point --recordings at a day folder (YYYY-MM-DD)")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_coverage_report(day_dir: Path) -> dict[str, Any] | None:
    path = day_dir / "coverage_report.json"
    if not path.is_file():
        return None
    return load_json(path)


def load_heartbeat(day_dir: Path) -> dict[str, Any] | None:
    path = day_dir / "heartbeat.json"
    if not path.is_file():
        return None
    return load_json(path)


def list_market_dirs(day_dir: Path, *, market_id: str | None = None) -> list[Path]:
    if market_id:
        path = day_dir / market_id
        if not path.is_dir():
            raise FileNotFoundError(f"market folder not found: {path}")
        return [path]
    return sorted(
        p
        for p in day_dir.iterdir()
        if p.is_dir() and p.name.startswith("btc_5m_") and (p / "manifest.json").is_file()
    )


def external_btc_dir(day_dir: Path) -> Path | None:
    path = day_dir / "external" / "btc_binance"
    return path if (path / "manifest.json").is_file() else None


def reference_prices_dir(day_dir: Path) -> Path | None:
    path = day_dir / "external" / "polymarket_rtds_chainlink"
    return path if (path / "manifest.json").is_file() else None


def iter_segment_paths(manifest: dict[str, Any], market_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for seg in manifest.get("segments") or []:
        rel = seg.get("path")
        if not rel:
            continue
        path = market_dir / str(rel)
        if path.is_file():
            paths.append(path)
    return paths


def iter_events_from_segments(
    segment_paths: list[Path],
    *,
    strict: bool,
    stats: EventReadStats | None = None,
) -> Iterator[MarketEvent]:
    st = stats if stats is not None else EventReadStats()
    for seg_path in segment_paths:
        for line in read_event_segment_lines(seg_path):
            try:
                data = json.loads(line)
                event = event_from_dict(data)
            except Exception as exc:
                st.corrupt_row_count += 1
                if strict:
                    raise ValueError(f"corrupt event row in {seg_path}: {exc}") from exc
                continue
            if event.event_id in st.seen_event_ids:
                st.duplicate_event_count += 1
                continue
            st.seen_event_ids.add(event.event_id)
            yield event


def iter_events_from_manifest(
    market_dir: Path,
    manifest: dict[str, Any],
    *,
    strict: bool,
    stats: EventReadStats | None = None,
) -> Iterator[MarketEvent]:
    return iter_events_from_segments(
        iter_segment_paths(manifest, market_dir),
        strict=strict,
        stats=stats,
    )


def payload_raw_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def iso_ts(dt) -> str | None:
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)
