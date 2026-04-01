"""Load v1 market allowlist from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from tyrex_pm.core.market_types import AllowlistEntry


def load_market_allowlist(path: str | Path) -> list[AllowlistEntry]:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not raw or "markets" not in raw:
        raise ValueError(f"{p}: missing top-level 'markets' list")
    rows: list[AllowlistEntry] = []
    for i, item in enumerate(raw["markets"]):
        if not isinstance(item, dict) or "slug" not in item:
            raise ValueError(f"{p}: markets[{i}] must be a mapping with 'slug'")
        slug = str(item["slug"]).strip()
        if not slug:
            raise ValueError(f"{p}: markets[{i}] empty slug")
        rows.append(AllowlistEntry(slug=slug, note=str(item.get("note") or "")))
    if len(rows) > 5:
        raise ValueError(f"{p}: v1 allows at most 5 markets, got {len(rows)}")
    if not rows:
        raise ValueError(f"{p}: at least one market required")
    return rows
