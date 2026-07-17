"""Mandatory acknowledgment gate (R7D.1) — never silently skipped."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from tyrex_pm.runtime.r7_position_ack import (
    AckError,
    PositionAcknowledgment,
    read_acknowledgment,
    validate_acknowledgment_against_inventory,
)
from tyrex_pm.runtime.r7_paths import resolve_acknowledgment_path


class AckGateError(RuntimeError):
    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f"{code}:{detail}")


@dataclass
class AckGateResult:
    ok: bool
    blockers: list[str] = field(default_factory=list)
    path: Path | None = None
    acknowledgment: PositionAcknowledgment | None = None
    artifact_id: str | None = None
    content_hash: str | None = None
    matched: int = 0
    notes: list[str] = field(default_factory=list)

    def to_report_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": None if self.path is None else str(self.path),
            "id": self.artifact_id,
            "content_hash": self.content_hash,
            "matched": self.matched,
            "blockers": list(self.blockers),
            "notes": list(self.notes),
            "untouched": True,
            "positions": (
                [
                    {
                        "token_suffix": p.token_id[-8:],
                        "condition_suffix": p.condition_id[-8:],
                        "quantity": p.quantity,
                        "category": p.category,
                        "identity": f"{p.condition_id[-8:]}|{p.token_id[-8:]}",
                    }
                    for p in (self.acknowledgment.positions if self.acknowledgment else [])
                ]
            ),
            "expected_count": (
                None
                if self.acknowledgment is None
                else self.acknowledgment.expected_position_count
            ),
        }


def _map_validation_blockers(raw: list[str]) -> list[str]:
    out: list[str] = []
    for b in raw:
        if b in {
            "ACK_INVENTORY_ROW_INCOMPLETE",
            "ACK_DUPLICATE_IDENTITY",
            "ACK_SOURCE_DISAGREEMENT",
        }:
            out.append(b)
        elif b in {
            "ACKNOWLEDGED_POSITION_SET_CHANGED",
            "UNACKNOWLEDGED_POSITION_PRESENT",
            "ACKNOWLEDGED_POSITION_BECAME_TRADABLE",
            "ACKNOWLEDGMENT_TEXT_MISMATCH",
        }:
            out.append("ACK_POSITION_SET_MISMATCH")
            out.append(b)
        else:
            out.append(b)
    return list(dict.fromkeys(out))


def enforce_acknowledgment_gate(
    *,
    acknowledgment_path: Path | None,
    raw_positions: Sequence[dict[str, Any]],
    repo_root: Path | None = None,
    selected_token_ids: Sequence[str] = (),
    selected_condition_id: str | None = None,
    ignore_selected_market_tokens: Sequence[str] = (),
    require_path: bool = True,
) -> AckGateResult:
    """Load and validate ack. Missing/invalid always blocks (dry and live)."""
    result = AckGateResult(ok=False)
    if acknowledgment_path is None and require_path:
        result.blockers.append("ACKNOWLEDGMENT_PATH_REQUIRED")
        return result
    if acknowledgment_path is None:
        result.blockers.append("ACKNOWLEDGMENT_PATH_REQUIRED")
        return result

    path = acknowledgment_path
    # If relative default under repo
    if not path.is_absolute() and not path.exists() and repo_root is not None:
        cand = repo_root / path
        if cand.exists() or path == resolve_acknowledgment_path(None, repo_root=repo_root):
            path = cand if cand.exists() else resolve_acknowledgment_path(None, repo_root=repo_root)

    result.path = path
    if not path.exists():
        result.blockers.append("ACKNOWLEDGMENT_ARTIFACT_MISSING")
        return result

    try:
        ack = read_acknowledgment(path)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, AckError, ValueError) as exc:
        result.blockers.append("ACKNOWLEDGMENT_ARTIFACT_INVALID")
        result.notes.append(f"{type(exc).__name__}")
        return result

    result.acknowledgment = ack
    result.artifact_id = ack.acknowledgment_id
    result.content_hash = ack.content_hash()

    if not raw_positions:
        result.blockers.append("ACK_INVENTORY_ROW_INCOMPLETE")
        result.notes.append("empty_inventory_evidence")
        return result

    v = validate_acknowledgment_against_inventory(
        ack,
        raw_positions=raw_positions,
        selected_token_ids=selected_token_ids,
        selected_condition_id=selected_condition_id,
        ignore_selected_market_tokens=ignore_selected_market_tokens,
    )
    result.matched = v.matched
    result.notes.extend(v.notes)
    if not v.ok:
        result.blockers.extend(_map_validation_blockers(v.blockers))
        return result

    result.ok = True
    return result
