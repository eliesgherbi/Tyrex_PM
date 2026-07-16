"""Z-Gap live scenario and operator-approval validation (A0.8)."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.runtime.config import (
    AppConfig,
    Z_GAP_ENTRY_MODE_ENFORCE,
    Z_GAP_ENTRY_MODE_OBSERVE_ONLY,
    ZGapStrategyConfig,
)
from tyrex_pm.runtime.z_gap_preflight import load_z_gap_preflight_gates
from tyrex_pm.strategies.z_gap.state import ZGapPhase, assert_startup_state_terminal_or_absent

_PLACEHOLDER_MARKERS = (
    "<required",
    "<required_",
    "placeholder",
    "todo",
    "fixme",
    "changeme",
    "xxx",
    "yyyymmdd",
)
_BTC_MARKET_RE = re.compile(r"^btc_5m_\d{8}_\d{4}$", re.IGNORECASE)
_MAX_ENFORCE_USD = Decimal("5")
_TIME_UNCERTAINTY_MAX_MS = 250.0
_PTB_ERROR_BPS_MAX = 0.5
_FEE_MODEL_ID_REQUIRED = "polymarket_dynamic_fd_v1"


@dataclass(frozen=True)
class PreflightValidationResult:
    errors: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_placeholder(value: object) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    lower = text.lower()
    return any(marker in lower for marker in _PLACEHOLDER_MARKERS)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def validate_operator_approval(
    data: dict[str, Any] | None,
    *,
    market_id: str,
    now_ts: float | None = None,
    max_usd: Decimal = _MAX_ENFORCE_USD,
) -> list[str]:
    errors: list[str] = []
    if not data:
        return ["operator_enforce_approval.json missing"]
    if not data.get("approved"):
        errors.append("operator approval not granted (approved != true)")
    scope = str(data.get("market_id") or data.get("event_scope") or "").strip()
    if not scope:
        errors.append("operator approval missing market_id/event_scope")
    elif scope != market_id:
        errors.append(f"operator approval scoped to {scope!r}, scenario market_id={market_id!r}")
    try:
        cap = Decimal(str(data.get("maximum_usd", max_usd)))
    except Exception:
        errors.append("operator approval maximum_usd invalid")
        cap = max_usd
    else:
        if cap > max_usd:
            errors.append(f"operator approval maximum_usd {cap} exceeds cap {max_usd}")
    if not data.get("one_trade_only", True):
        errors.append("operator approval must set one_trade_only=true for Phase A tiny live")
    exp_raw = data.get("expiration_ts") or data.get("expires_at")
    now = now_ts if now_ts is not None else time.time()
    if exp_raw is None:
        errors.append("operator approval missing expiration_ts/expires_at")
    else:
        try:
            exp = float(exp_raw)
        except (TypeError, ValueError):
            errors.append(f"operator approval expiration invalid: {exp_raw!r}")
        else:
            if exp < now:
                errors.append(f"operator approval expired at {exp} (now={now:.0f})")
    return errors


def validate_calibration_review(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return ["calibration_lite_review.json missing"]
    if not data.get("reviewed"):
        return ["calibration_lite_review.reviewed must be true"]
    if not (data.get("operator_signoff") or data.get("reviewed_by")):
        return ["calibration_lite_review requires operator_signoff or reviewed_by"]
    status = str(data.get("status", "")).strip().lower()
    if status and status not in {"accepted_for_tiny_live", "accepted", "reviewed"}:
        return [f"calibration_lite_review.status not accepted for tiny live ({status!r})"]
    return []


def validate_ptb_attestation_fresh(
    data: dict[str, Any] | None,
    *,
    pre_boundary: bool = False,
) -> list[str]:
    if not data:
        if pre_boundary:
            return []
        return ["ptb_attestation.json missing"]
    if pre_boundary and not data.get("attestation_pass"):
        status = str(data.get("boundary_status") or "WAITING_FOR_BOUNDARY")
        if status in {"WAITING_FOR_BOUNDARY", "PTB_PENDING"}:
            return []
    if not data.get("attestation_pass"):
        return ["ptb_attestation.attestation_pass must be true"]
    if data.get("golden_fixture_only"):
        return ["ptb_attestation golden_fixture_only=true blocks enforce"]
    err = data.get("ptb_error_bps")
    if err is not None:
        try:
            if float(err) > _PTB_ERROR_BPS_MAX:
                return [f"ptb_error_bps {err} exceeds {_PTB_ERROR_BPS_MAX}"]
        except (TypeError, ValueError):
            return [f"invalid ptb_error_bps: {err!r}"]
    return []


def validate_clock_artifact(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return ["clock_sanity.json missing"]
    if data.get("sync_status") != "synced":
        return [f"clock sync_status must be synced (got {data.get('sync_status')!r})"]
    unc = data.get("time_authority_uncertainty_ms")
    if unc is None:
        return ["clock_sanity missing time_authority_uncertainty_ms"]
    try:
        if float(unc) > _TIME_UNCERTAINTY_MAX_MS:
            return [f"time_authority uncertainty_ms {unc} exceeds {_TIME_UNCERTAINTY_MAX_MS}"]
    except (TypeError, ValueError):
        return [f"invalid time_authority_uncertainty_ms: {unc!r}"]
    return []


def validate_z_gap_strategy_safety(zg: ZGapStrategyConfig, raw_zg: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if zg.entry_mode != Z_GAP_ENTRY_MODE_ENFORCE:
        errors.append(f"entry_mode must be enforce (got {zg.entry_mode!r})")
    sizing = zg.sizing
    if sizing is None:
        errors.append("sizing config required")
    elif sizing.mode != "fixed_usd":
        errors.append(f"sizing.mode must be fixed_usd (got {sizing.mode!r})")
    elif sizing.max_usd > _MAX_ENFORCE_USD:
        errors.append(f"sizing.max_usd {sizing.max_usd} exceeds {_MAX_ENFORCE_USD}")
    entry = zg.entry
    if entry is None:
        errors.append("entry config required")
    else:
        if not entry.one_position_per_window:
            errors.append("entry.one_position_per_window must be true")
        if not entry.no_reentry_after_exit:
            errors.append("entry.no_reentry_after_exit must be true")
    exit_cfg = zg.exit
    if exit_cfg is None:
        errors.append("exit config required")
    else:
        if exit_cfg.flatten_before_event_end_s < 20:
            errors.append("exit.flatten_before_event_end_s must be >= 20")
        if exit_cfg.max_exit_attempts <= 0:
            errors.append("exit.max_exit_attempts must be configured > 0")
        if exit_cfg.retry_interval_ms <= 0:
            errors.append("exit.retry_interval_ms must be configured > 0")
    hold = raw_zg.get("hold_to_resolution")
    if isinstance(hold, dict) and hold.get("enabled"):
        errors.append("hold_to_resolution.enabled blocks Phase A enforce")
    return errors


def validate_market_readiness(
    zg: ZGapStrategyConfig,
    *,
    now_ts: float | None = None,
    min_prestart_s: float = 20.0,
    lifecycle_state_path: Path | None = None,
) -> list[str]:
    errors: list[str] = []
    now = now_ts if now_ts is not None else time.time()
    if _is_placeholder(zg.market_id) or not _BTC_MARKET_RE.match(zg.market_id):
        errors.append(f"market_id must be real btc_5m id (got {zg.market_id!r})")
    if _is_placeholder(zg.condition_id):
        errors.append("condition_id required")
    if _is_placeholder(zg.yes_token_id):
        errors.append("yes_token_id required")
    if _is_placeholder(zg.no_token_id):
        errors.append("no_token_id required")
    if zg.event_start_ts is None:
        errors.append("event_start_ts required")
    if zg.event_end_ts is None:
        errors.append("event_end_ts required")
    if zg.event_start_ts is not None and zg.event_end_ts is not None:
        if zg.event_end_ts <= zg.event_start_ts:
            errors.append("event_end_ts must be > event_start_ts")
        if now > float(zg.event_start_ts) - min_prestart_s:
            errors.append(
                f"insufficient prestart time (now={now:.0f}, event_start_ts={zg.event_start_ts})"
            )
        if float(zg.event_end_ts) < now - 300:
            errors.append("event_end_ts is in the past")
    if lifecycle_state_path is not None and lifecycle_state_path.is_file():
        try:
            raw = json.loads(lifecycle_state_path.read_text(encoding="utf-8"))
            assert_startup_state_terminal_or_absent(raw)
            phase = str(raw.get("phase", ""))
            if phase not in {ZGapPhase.DONE.value, ZGapPhase.FAILED.value, ZGapPhase.IDLE.value}:
                errors.append(f"non-terminal persisted lifecycle state: {phase}")
        except Exception as exc:
            errors.append(f"persisted lifecycle state invalid: {exc}")
    return errors


def validate_z_gap_live_scenario(
    app: AppConfig,
    *,
    artifacts_dir: Path | str | None = None,
    raw_strategy_z_gap: dict[str, Any] | None = None,
    now_ts: float | None = None,
    lifecycle_state_path: Path | None = None,
    pre_boundary: bool = False,
    observe_only: bool = False,
) -> PreflightValidationResult:
    """Validate a proposed Z-Gap tiny-live enforce scenario."""
    errors: list[str] = []
    warnings: list[str] = []
    zg = app.z_gap
    if zg is None:
        return PreflightValidationResult(errors=("z_gap config missing",))

    base = Path(artifacts_dir) if artifacts_dir is not None else Path("var/reporting/z_gap")
    gates = load_z_gap_preflight_gates(
        base,
        phase="static" if pre_boundary else "full",
        pre_boundary=pre_boundary,
    )
    errors.extend(gates.blockers)

    raw_zg = raw_strategy_z_gap or {}
    if observe_only:
        if zg.entry_mode != Z_GAP_ENTRY_MODE_OBSERVE_ONLY:
            errors.append(f"entry_mode must be observe_only (got {zg.entry_mode!r})")
    else:
        errors.extend(validate_z_gap_strategy_safety(zg, raw_zg))
    errors.extend(validate_market_readiness(zg, now_ts=now_ts, lifecycle_state_path=lifecycle_state_path))

    if pre_boundary:
        ptb_data = _read_json(base / "ptb_attestation.json")
        if ptb_data is None:
            warnings.append("ptb_attestation: WAITING_FOR_BOUNDARY (not required before boundary)")
        else:
            ptb_warnings = validate_ptb_attestation_fresh(ptb_data, pre_boundary=True)
            warnings.extend(ptb_warnings)
    else:
        errors.extend(validate_ptb_attestation_fresh(_read_json(base / "ptb_attestation.json")))
    errors.extend(validate_clock_artifact(_read_json(base / "clock_sanity.json")))
    errors.extend(validate_calibration_review(_read_json(base / "calibration_lite_review.json")))
    if not observe_only:
        errors.extend(
            validate_operator_approval(
                _read_json(base / "operator_enforce_approval.json"),
                market_id=zg.market_id,
                now_ts=now_ts,
                max_usd=zg.sizing.max_usd if zg.sizing else _MAX_ENFORCE_USD,
            )
        )

    fee = _read_json(base / "fee_curve_spike.json")
    if fee is None:
        errors.append("fee_curve_spike.json missing")
    elif not (fee.get("spike_passed") or fee.get("fd")):
        warnings.append("fee_curve_spike artifact present but fd curve not confirmed")

    return PreflightValidationResult(errors=tuple(errors), warnings=tuple(warnings))
