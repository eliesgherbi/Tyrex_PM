"""PTB commissioning certificate policy for Z-Gap boundary attestation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from typing import Any

COMMISSIONING_POLICY_NAME = "z_gap_ptb_commissioning_v1"
DEFAULT_MIN_WINDOWS = 3
DEFAULT_MAX_ERROR_BPS = 0.5
DEFAULT_MAX_AGE_HOURS = 24.0


@dataclass(frozen=True)
class CommissioningValidation:
    valid: bool
    reason: str
    policy_name: str = COMMISSIONING_POLICY_NAME
    windows_validated: int = 0


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def validate_commissioning_certificate(
    data: dict[str, Any] | None,
    *,
    ptb_config_hash: str,
    now_ts: float,
    min_windows: int = DEFAULT_MIN_WINDOWS,
    max_error_bps: float = DEFAULT_MAX_ERROR_BPS,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
) -> CommissioningValidation:
    if not data:
        return CommissioningValidation(valid=False, reason="missing commissioning certificate")
    if data.get("policy_name") != COMMISSIONING_POLICY_NAME and data.get("policy_id") != COMMISSIONING_POLICY_NAME:
        return CommissioningValidation(valid=False, reason="unknown commissioning policy")
    if data.get("status") != "valid":
        return CommissioningValidation(valid=False, reason=f"certificate status {data.get('status')!r}")
    if data.get("ptb_config_hash") != ptb_config_hash:
        return CommissioningValidation(valid=False, reason="commissioning hash mismatch")
    windows = data.get("windows") or []
    if not isinstance(windows, list) or len(windows) < min_windows:
        return CommissioningValidation(
            valid=False,
            reason=f"insufficient completed windows ({len(windows) if isinstance(windows, list) else 0} < {min_windows})",
        )
    for row in windows:
        if not isinstance(row, dict):
            return CommissioningValidation(valid=False, reason="invalid window row in certificate")
        err = row.get("ptb_error_bps")
        if err is not None and float(err) > max_error_bps:
            return CommissioningValidation(valid=False, reason=f"window error {err} bps exceeds {max_error_bps}")
        if row.get("source_mismatch_unresolved"):
            return CommissioningValidation(valid=False, reason="unresolved source mismatch in certificate")
    issued = data.get("issued_at_ts")
    if issued is not None:
        age_h = (now_ts - float(issued)) / 3600.0
        if age_h > max_age_hours:
            return CommissioningValidation(valid=False, reason=f"certificate stale ({age_h:.1f}h > {max_age_hours}h)")
    return CommissioningValidation(
        valid=True,
        reason="commissioning certificate valid",
        windows_validated=len(windows),
    )


def load_commissioning_certificate(path: Path | str | None = None) -> dict[str, Any] | None:
    base = Path(path) if path is not None else Path("var/reporting/z_gap/ptb_commissioning_certificate.json")
    return _read_json(base)


def independent_ptb_reference_available() -> bool:
    """Post-window independent reference via Chainlink aggregator V3 eth_call."""
    import os

    return bool(os.environ.get("TYREX_ETH_RPC_URL") or os.environ.get("ETH_RPC_URL"))


def build_commissioning_certificate(
    *,
    policy_id: str,
    implementation_hash: str,
    config_hash: str,
    windows: list[dict[str, Any]],
    windows_required: int,
    now_ts: float,
    expiry_hours: float = DEFAULT_MAX_AGE_HOURS,
    independent_source: str,
) -> dict[str, Any]:
    usable = [w for w in windows if w.get("usable")]
    errors = [float(w["ptb_error_bps"]) for w in usable if w.get("ptb_error_bps") is not None]
    all_ok = all(e <= DEFAULT_MAX_ERROR_BPS for e in errors) if errors else False
    mismatch_count = sum(1 for w in usable if w.get("source_mismatch_unresolved"))
    status = "valid" if len(usable) >= windows_required and all_ok and mismatch_count == 0 else "invalid"
    return {
        "policy_id": policy_id,
        "policy_name": policy_id,
        "implementation_hash": implementation_hash,
        "ptb_config_hash": config_hash,
        "config_hash": config_hash,
        "windows_required": windows_required,
        "windows_usable": len(usable),
        "windows": windows,
        "maximum_error_bps": DEFAULT_MAX_ERROR_BPS,
        "all_errors_within_0_5_bps": all_ok,
        "source_mismatch_count": mismatch_count,
        "independent_reference_source": independent_source,
        "created_at": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
        "issued_at_ts": now_ts,
        "expires_at": datetime.fromtimestamp(now_ts + expiry_hours * 3600.0, tz=timezone.utc).isoformat(),
        "expires_at_ts": now_ts + expiry_hours * 3600.0,
        "status": status,
    }


def evaluate_commissioning_window(
    *,
    market_id: str,
    event_start_ts: float,
    event_end_ts: float,
    locked_k: str,
    live_k: str | None,
    log_k: str | None,
    live_log_difference_bps: str | None,
    independent: Any,
) -> dict[str, Any]:
    """Score one completed window for PTB commissioning certificate."""
    from tyrex_pm.runtime.z_gap_ptb_attestation import ptb_error_bps

    row: dict[str, Any] = {
        "market_id": market_id,
        "event_start_ts": event_start_ts,
        "event_end_ts": event_end_ts,
        "locked_k": locked_k,
        "live_k": live_k,
        "log_k": log_k,
        "live_log_difference_bps": live_log_difference_bps,
        "independent_reference": independent.to_dict() if hasattr(independent, "to_dict") else independent,
        "usable": False,
        "exclude_reason": None,
        "ptb_error_bps": None,
        "source_mismatch_unresolved": False,
    }
    if live_k and log_k and live_log_difference_bps is not None:
        try:
            if Decimal(str(live_log_difference_bps)) > Decimal(str(DEFAULT_MAX_ERROR_BPS)):
                row["source_mismatch_unresolved"] = True
                row["exclude_reason"] = "live_log_agreement_exceeded"
        except Exception:
            row["source_mismatch_unresolved"] = True
            row["exclude_reason"] = "live_log_agreement_parse_error"

    if not getattr(independent, "available", False):
        row["exclude_reason"] = row["exclude_reason"] or getattr(independent, "fail_reason", "independent_unavailable")
        return row

    ref_price = getattr(independent, "price", None)
    if ref_price is None:
        row["exclude_reason"] = row["exclude_reason"] or "independent_price_missing"
        return row

    try:
        err = ptb_error_bps(Decimal(locked_k), Decimal(str(ref_price)))
        row["ptb_error_bps"] = str(err)
        if err > Decimal(str(DEFAULT_MAX_ERROR_BPS)):
            row["exclude_reason"] = row["exclude_reason"] or f"independent_error_{err}_bps"
            return row
    except Exception as exc:
        row["exclude_reason"] = row["exclude_reason"] or str(exc)
        return row

    if row["source_mismatch_unresolved"]:
        return row

    row["usable"] = True
    return row


def write_commissioning_report(path: Path, *, certificate: dict[str, Any], independent_doc: dict[str, Any]) -> None:
    """Write human-readable PTB commissioning report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# PTB Commissioning Report",
        "",
        f"**Status:** {certificate.get('status')}",
        f"**Policy:** {certificate.get('policy_id')}",
        f"**Created:** {certificate.get('created_at')}",
        f"**Expires:** {certificate.get('expires_at')}",
        "",
        "## Independent reference source",
        "",
        f"- **Source:** {independent_doc.get('source')}",
        f"- **Endpoint:** {independent_doc.get('endpoint')}",
        f"- **Field:** {independent_doc.get('field_name')}",
        f"- **Timestamp semantics:** {independent_doc.get('timestamp_semantics')}",
        f"- **Availability delay:** {independent_doc.get('availability_delay')}",
        f"- **Failure behavior:** {independent_doc.get('failure_behavior')}",
        f"- **Why independent:** {independent_doc.get('why_independent')}",
        "",
        "## Window evidence",
        "",
    ]
    for w in certificate.get("windows") or []:
        lines.append(
            f"- `{w.get('market_id')}` usable={w.get('usable')} "
            f"error_bps={w.get('ptb_error_bps')} exclude={w.get('exclude_reason')}"
        )
    lines.extend(
        [
            "",
            "## Certificate summary",
            "",
            f"- windows_required: {certificate.get('windows_required')}",
            f"- windows_usable: {certificate.get('windows_usable')}",
            f"- all_errors_within_0_5_bps: {certificate.get('all_errors_within_0_5_bps')}",
            f"- source_mismatch_count: {certificate.get('source_mismatch_count')}",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_commissioning_certificate(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
