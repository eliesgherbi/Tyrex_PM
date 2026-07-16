"""Version/config hash helpers for Z-Gap calibration acknowledgment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tyrex_pm.runtime.config import AppConfig, ZGapStrategyConfig


@dataclass(frozen=True)
class ZGapConfigFingerprint:
    strategy_version: str
    model_config_hash: str
    parameter_hash: str

    @property
    def combined_hash(self) -> str:
        payload = f"{self.strategy_version}|{self.model_config_hash}|{self.parameter_hash}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def compute_z_gap_config_fingerprint(
    app: AppConfig,
    *,
    strategy_file: Path | str | None = None,
    scenario_file: Path | str | None = None,
) -> ZGapConfigFingerprint:
    zg = app.z_gap
    if zg is None:
        raise ValueError("z_gap config missing")

    strategy_version = "z_gap_phase_a"
    if strategy_file is not None and Path(strategy_file).is_file():
        strategy_version = hashlib.sha256(Path(strategy_file).read_bytes()).hexdigest()[:12]

    model_keys = (
        "sigma",
        "entry",
        "sizing",
        "exit",
        "reconciliation",
        "live_validation",
    )
    model_blob = {k: _extract_zg_section(zg, k) for k in model_keys}
    model_config_hash = hashlib.sha256(_stable_json(model_blob).encode("utf-8")).hexdigest()[:16]

    param_blob = {
        "entry_mode": zg.entry_mode,
        "scenario": str(scenario_file) if scenario_file else None,
    }
    parameter_hash = hashlib.sha256(_stable_json(param_blob).encode("utf-8")).hexdigest()[:16]
    return ZGapConfigFingerprint(
        strategy_version=strategy_version,
        model_config_hash=model_config_hash,
        parameter_hash=parameter_hash,
    )


def _extract_zg_section(zg: ZGapStrategyConfig, key: str) -> Any:
    value = getattr(zg, key, None)
    if value is None:
        return None
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    return value


def calibration_ack_matches_fingerprint(
    data: dict[str, Any] | None,
    fingerprint: ZGapConfigFingerprint,
    *,
    now_ts: float,
    expiry_hours: float = 168.0,
) -> bool:
    if not data or not data.get("reviewed"):
        return False
    if not (data.get("operator_signoff") or data.get("operator_confirmed")):
        return False
    if data.get("model_config_hash") != fingerprint.model_config_hash:
        return False
    if data.get("parameter_hash") and data.get("parameter_hash") != fingerprint.parameter_hash:
        return False
    exp = data.get("expires_at_ts") or data.get("expiry_ts")
    if exp is not None:
        try:
            if float(exp) < now_ts:
                return False
        except (TypeError, ValueError):
            return False
    reviewed_at = data.get("reviewed_at_ts")
    if reviewed_at is not None and expiry_hours > 0:
        try:
            age_h = (now_ts - float(reviewed_at)) / 3600.0
            if age_h > expiry_hours:
                return False
        except (TypeError, ValueError):
            return False
    return True
