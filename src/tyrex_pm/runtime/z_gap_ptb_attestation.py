"""PTB attestation helpers for Z-Gap Phase A preflight (A0.1)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

PTB_TOLERANCE_BPS = Decimal("0.5")
GOLDEN_DAY_MARKER = "golden_day"


@dataclass(frozen=True)
class PtbAttestationReport:
    market_id: str
    event_start_ts: float | None
    K_derived: str | None
    K_reference: str | None
    ptb_error_bps: str | None
    reference_source: str
    data_source: str
    attestation_pass: bool
    attestation_fail_reason: str | None
    golden_fixture_only: bool
    enforce_unlock_allowed: bool
    checked_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ptb_error_bps(K_derived: Decimal, K_reference: Decimal) -> Decimal:
    """Absolute PTB error in basis points relative to reference."""
    if K_reference <= 0:
        raise ValueError("K_reference must be positive")
    return (abs(K_derived - K_reference) / K_reference) * Decimal("10000")


def _iter_jsonl_events(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _find_ptb_in_recording(recording_dir: Path, *, market_id: str | None) -> dict[str, Any] | None:
    for path in sorted(recording_dir.rglob("events*.jsonl")):
        for event in _iter_jsonl_events(path):
            if event.get("event_type") != "price_to_beat_observed":
                continue
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            mid = str(payload.get("market_id") or event.get("market_id") or "").strip()
            if market_id and mid and mid != market_id:
                continue
            return payload
    return None


def _is_golden_day_path(path: Path) -> bool:
    parts = {p.lower() for p in path.parts}
    return GOLDEN_DAY_MARKER in parts


def derive_k_from_recording(
    recording_dir: Path,
    *,
    market_id: str | None = None,
) -> tuple[Decimal | None, float | None, str, bool]:
    """Return (K_derived, event_start_ts, data_source_label, golden_fixture_only)."""
    golden = _is_golden_day_path(recording_dir)
    payload = _find_ptb_in_recording(recording_dir, market_id=market_id)
    if payload is None:
        label = f"{GOLDEN_DAY_MARKER}_recording" if golden else "live_recording"
        return None, None, label, golden
    raw_k = payload.get("price_to_beat")
    if raw_k in (None, ""):
        return None, payload.get("event_start_ts"), "live_recording", golden
    try:
        k = Decimal(str(raw_k))
    except Exception:
        return None, payload.get("event_start_ts"), "live_recording", golden
    start_ts = payload.get("event_start_ts")
    try:
        start_f = float(start_ts) if start_ts is not None else None
    except (TypeError, ValueError):
        start_f = None
    label = f"{GOLDEN_DAY_MARKER}_recording" if golden else "live_recording"
    return k, start_f, label, golden


def evaluate_ptb_attestation(
    *,
    K_derived: Decimal | None,
    K_reference: Decimal | None,
    reference_source: str,
    market_id: str,
    event_start_ts: float | None,
    data_source: str,
    golden_fixture_only: bool,
    checked_at_utc: str,
) -> PtbAttestationReport:
    """Evaluate PTB attestation with fail-closed rules."""
    fail_reason: str | None = None
    err_bps: Decimal | None = None
    passed = False
    enforce_unlock = False

    if K_reference is None:
        fail_reason = "missing K_reference (no fresh PTB reference available)"
    elif K_derived is None:
        fail_reason = "missing K_derived (no price_to_beat in recording)"
    else:
        err_bps = ptb_error_bps(K_derived, K_reference)
        if err_bps > PTB_TOLERANCE_BPS:
            fail_reason = f"ptb_error_bps {err_bps} exceeds tolerance {PTB_TOLERANCE_BPS}"
        else:
            passed = True
            enforce_unlock = not golden_fixture_only

    if passed and golden_fixture_only:
        enforce_unlock = False

    return PtbAttestationReport(
        market_id=market_id,
        event_start_ts=event_start_ts,
        K_derived=str(K_derived) if K_derived is not None else None,
        K_reference=str(K_reference) if K_reference is not None else None,
        ptb_error_bps=str(err_bps) if err_bps is not None else None,
        reference_source=reference_source,
        data_source=data_source,
        attestation_pass=passed,
        attestation_fail_reason=fail_reason,
        golden_fixture_only=golden_fixture_only,
        enforce_unlock_allowed=enforce_unlock,
        checked_at_utc=checked_at_utc,
    )


def write_ptb_attestation_report(path: Path, report: PtbAttestationReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
