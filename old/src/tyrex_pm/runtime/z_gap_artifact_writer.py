"""Auto-generate Z-Gap technical preflight artifacts from live truth."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tyrex_pm.quant.fees import FeeModel
from tyrex_pm.runtime.btc_5m_metadata import Btc5mMarketMetadata
from tyrex_pm.runtime.z_gap_config_hash import ZGapConfigFingerprint


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_fee_curve_artifact(
    path: Path,
    *,
    fee_model: FeeModel,
    market_id: str,
    condition_id: str | None,
    config_hash: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "generated_at": _utc_now_iso(),
        "source": fee_model.source,
        "market_id": market_id,
        "condition_id": condition_id,
        "config_hash": config_hash,
        "status": "resolved" if fee_model.is_resolved else "unknown",
        "outcome": "full_curve_available" if fee_model.is_resolved else "unknown",
        "fd": {
            "r": float(fee_model.fd_r) if fee_model.fd_r is not None else None,
            "e": float(fee_model.fd_e) if fee_model.fd_e is not None else None,
            "to": fee_model.fd_to,
        },
        "evidence": {
            "fee_model_id": fee_model.fee_model_id,
            "fee_model_status": fee_model.fee_model_status,
        },
        "freshness_s": 300,
    }
    if fee_model.is_resolved:
        payload["spike_passed"] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def write_market_metadata_artifact(
    path: Path,
    *,
    meta: Btc5mMarketMetadata,
    config_hash: str,
) -> dict[str, Any]:
    payload = {
        "generated_at": _utc_now_iso(),
        "source": "gamma_event_metadata",
        "market_id": meta.market_id,
        "condition_id": meta.condition_id,
        "config_hash": config_hash,
        "status": "resolved",
        "event_url": meta.event_url,
        "event_slug": meta.event_slug,
        "event_start_ts": meta.event_start_ts,
        "event_end_ts": meta.event_end_ts,
        "yes_token_id": meta.yes_token_id,
        "no_token_id": meta.no_token_id,
        "evidence": {"event_title": meta.event_title},
        "freshness_s": 300,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def write_connectivity_artifact(path: Path, report: Any) -> dict[str, Any]:
    if hasattr(report, "__dataclass_fields__"):
        payload = asdict(report)
    elif isinstance(report, dict):
        payload = dict(report)
    else:
        payload = {"ok": bool(getattr(report, "ok", False))}
    payload["generated_at"] = _utc_now_iso()
    payload["source"] = "binance_ws_preflight"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def write_clock_artifact(path: Path, report: Any) -> dict[str, Any]:
    payload = report.to_dict() if hasattr(report, "to_dict") else dict(report)
    payload["generated_at"] = _utc_now_iso()
    payload["source"] = "time_authority_sample"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def write_sidecar_health_artifact(
    path: Path,
    *,
    healthy: bool,
    market_id: str,
    tick_age_s: float | None,
    process_running: bool,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "generated_at": _utc_now_iso(),
        "source": "chainlink_tick_logger",
        "market_id": market_id,
        "status": "healthy" if healthy else "unhealthy",
        "healthy": healthy,
        "tick_age_s": tick_age_s,
        "process_running": process_running,
        "evidence": details or {},
        "freshness_s": 60,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def write_ptb_waiting_artifact(path: Path, *, market_id: str, event_start_ts: float) -> dict[str, Any]:
    payload = {
        "generated_at": _utc_now_iso(),
        "source": "session_orchestrator",
        "market_id": market_id,
        "event_start_ts": event_start_ts,
        "boundary_status": "WAITING_FOR_BOUNDARY",
        "attestation_pass": False,
        "enforce_unlock_allowed": False,
        "status": "waiting_for_boundary",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def write_calibration_ack_artifact(
    path: Path,
    *,
    fingerprint: ZGapConfigFingerprint,
    now_ts: float,
    expiry_hours: float,
    maximum_risk_usd: str = "5",
) -> dict[str, Any]:
    expiry_ts = now_ts + expiry_hours * 3600.0
    payload = {
        "reviewed": True,
        "operator_confirmed": True,
        "operator_signoff": True,
        "reviewed_by": "operator_interactive",
        "reviewed_at": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
        "reviewed_at_ts": now_ts,
        "expires_at_ts": expiry_ts,
        "status": "accepted_for_tiny_live",
        "strategy_version": fingerprint.strategy_version,
        "model_config_hash": fingerprint.model_config_hash,
        "parameter_hash": fingerprint.parameter_hash,
        "maximum_approved_experimental_risk_usd": maximum_risk_usd,
        "notes": "Operator-confirmed tiny operational test; not proof of profitability.",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def write_operator_approval_artifact(
    path: Path,
    *,
    market_id: str,
    condition_id: str,
    run_name: str,
    event_start_ts: float,
    event_end_ts: float,
    now_ts: float,
    maximum_usd: str = "5",
) -> dict[str, Any]:
    payload = {
        "approved": True,
        "market_id": market_id,
        "condition_id": condition_id,
        "run_name": run_name,
        "maximum_usd": maximum_usd,
        "one_trade_only": True,
        "maximum_entries": 1,
        "maximum_positions": 1,
        "no_reentry": True,
        "stop_after_terminal": True,
        "approval_ts": now_ts,
        "expiration_ts": event_end_ts + 300,
        "event_start_ts": event_start_ts,
        "event_end_ts": event_end_ts,
        "manual_kill_acknowledged": True,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def artifact_stale_for_market(data: dict[str, Any] | None, *, market_id: str) -> bool:
    if not data:
        return True
    scope = str(data.get("market_id") or "").strip()
    return bool(scope and scope != market_id)
