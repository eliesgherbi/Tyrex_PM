"""Load fixture-only resolution evidence (no network)."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.core.ids import MarketId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.domain.polymarket.resolution_evidence import (
    ResolutionEvidence,
    ResolutionEvidenceStatus,
)


def _parse_ts(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw
    text = str(raw)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _parse_side(raw: Any) -> OutcomeSide | None:
    if raw is None:
        return None
    text = str(raw).upper()
    if text in {"YES", "UP"}:
        return OutcomeSide.YES
    if text in {"NO", "DOWN"}:
        return OutcomeSide.NO
    raise ValueError(f"unknown resolved_side {raw!r}")


def resolution_evidence_from_mapping(data: dict[str, Any]) -> ResolutionEvidence:
    status_raw = str(data.get("status", "READY")).upper()
    status = ResolutionEvidenceStatus(status_raw)
    return ResolutionEvidence(
        market_id=MarketId(str(data["market_id"])),
        window_id=str(data["window_id"]),
        boundary_k=Decimal(str(data["boundary_k"])),
        settlement_price=(
            None if data.get("settlement_price") is None else Decimal(str(data["settlement_price"]))
        ),
        resolved_side=_parse_side(data.get("resolved_side")),
        observed_at=_parse_ts(data["observed_at"]),
        source=str(data.get("source", "fixture")),
        provenance=str(data.get("provenance", "fixture")),
        status=status,
        quality_note=None if data.get("quality_note") is None else str(data["quality_note"]),
        evidence_id=str(data.get("evidence_id") or data.get("id") or ""),
        extras=dict(data.get("extras") or {}),
    )


def load_resolution_evidence_events(
    path: Path,
) -> list[tuple[datetime, ResolutionEvidence]]:
    """Load timed evidence rows from a fixture JSON file or embedded section."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("resolution_evidence_events")
    if rows is None and "evidence" in payload:
        rows = [{"ts_received": payload.get("observed_at"), "evidence": payload}]
    if not rows:
        return []
    out: list[tuple[datetime, ResolutionEvidence]] = []
    for row in rows:
        body = row.get("evidence") or row
        ts = _parse_ts(row.get("ts_received") or body.get("observed_at"))
        out.append((ts, resolution_evidence_from_mapping(body)))
    out.sort(key=lambda item: item[0])
    return out
