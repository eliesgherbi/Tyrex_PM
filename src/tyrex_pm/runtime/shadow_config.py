"""R5 shadow OMS / persistence configuration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True, kw_only=True)
class ShadowConfig:
    enable_oms: bool
    max_position_notional: Decimal
    max_total_exposure: Decimal
    max_hold: timedelta
    flatten_before_close: timedelta
    exit_on_flat: bool
    persistence_path: Path
    cancel_unfilled_residual: bool = False
    fee_rate: Decimal = Decimal("0")
    fee_model_id: str = "shadow_zero_fee_v1"

    def __post_init__(self) -> None:
        if self.max_position_notional <= 0 or self.max_total_exposure <= 0:
            raise ValueError("exposure caps must be > 0")
        if self.max_hold <= timedelta(0):
            raise ValueError("max_hold must be > 0")
        if self.flatten_before_close < timedelta(0):
            raise ValueError("flatten_before_close must be >= 0")
        if self.fee_rate < 0:
            raise ValueError("fee_rate must be >= 0")

    def fingerprint(self) -> str:
        payload = {
            "enable_oms": self.enable_oms,
            "max_position_notional": str(self.max_position_notional),
            "max_total_exposure": str(self.max_total_exposure),
            "max_hold_s": self.max_hold.total_seconds(),
            "flatten_before_close_s": self.flatten_before_close.total_seconds(),
            "exit_on_flat": self.exit_on_flat,
            "persistence_path": str(self.persistence_path),
            "cancel_unfilled_residual": self.cancel_unfilled_residual,
            "fee_rate": str(self.fee_rate),
            "fee_model_id": self.fee_model_id,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def shadow_config_from_mapping(data: Mapping[str, Any]) -> ShadowConfig:
    required = (
        "enable_oms",
        "max_position_notional",
        "max_total_exposure",
        "max_hold_s",
        "flatten_before_close_s",
        "exit_on_flat",
        "persistence_path",
    )
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"shadow config missing: {missing}")
    return ShadowConfig(
        enable_oms=bool(data["enable_oms"]),
        max_position_notional=Decimal(str(data["max_position_notional"])),
        max_total_exposure=Decimal(str(data["max_total_exposure"])),
        max_hold=timedelta(seconds=float(data["max_hold_s"])),
        flatten_before_close=timedelta(seconds=float(data["flatten_before_close_s"])),
        exit_on_flat=bool(data["exit_on_flat"]),
        persistence_path=Path(str(data["persistence_path"])),
        cancel_unfilled_residual=bool(data.get("cancel_unfilled_residual", False)),
        fee_rate=Decimal(str(data.get("fee_rate", "0"))),
        fee_model_id=str(data.get("fee_model_id", "shadow_zero_fee_v1")),
    )
