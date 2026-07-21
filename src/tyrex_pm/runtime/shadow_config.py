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
    # R5.1 retry policy
    entry_retry_cooldown_s: float = 5.0
    entry_max_attempts: int = 3
    exit_retry_cooldown_s: float = 3.0
    exit_max_normal_retries: int = 3
    exit_escalate_after: int = 2
    # N5 fill model (fixture-only when set; not production defaults)
    fill_model_id: str = "shadow_immediate_visible_depth_v0"
    fill_latency_ms: float = 0.0
    fill_extra_slip_ticks: Decimal = Decimal("0")
    fill_tick_size: Decimal = Decimal("0.01")
    # Prepared-next promotion: require confirmed FLAT (N5)
    require_flat_for_promote: bool = True

    def __post_init__(self) -> None:
        if self.max_position_notional <= 0 or self.max_total_exposure <= 0:
            raise ValueError("exposure caps must be > 0")
        if self.max_hold <= timedelta(0):
            raise ValueError("max_hold must be > 0")
        if self.flatten_before_close < timedelta(0):
            raise ValueError("flatten_before_close must be >= 0")
        if self.fee_rate < 0:
            raise ValueError("fee_rate must be >= 0")
        if self.entry_retry_cooldown_s <= 0 or self.exit_retry_cooldown_s <= 0:
            raise ValueError("retry cooldowns must be > 0")
        if self.entry_max_attempts < 1 or self.exit_max_normal_retries < 1:
            raise ValueError("retry attempt caps must be >= 1")
        if self.fill_latency_ms < 0:
            raise ValueError("fill_latency_ms must be >= 0")
        if self.fill_extra_slip_ticks < 0:
            raise ValueError("fill_extra_slip_ticks must be >= 0")

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
            "entry_retry_cooldown_s": self.entry_retry_cooldown_s,
            "entry_max_attempts": self.entry_max_attempts,
            "exit_retry_cooldown_s": self.exit_retry_cooldown_s,
            "exit_max_normal_retries": self.exit_max_normal_retries,
            "exit_escalate_after": self.exit_escalate_after,
            "fill_model_id": self.fill_model_id,
            "fill_latency_ms": self.fill_latency_ms,
            "fill_extra_slip_ticks": str(self.fill_extra_slip_ticks),
            "fill_tick_size": str(self.fill_tick_size),
            "require_flat_for_promote": self.require_flat_for_promote,
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
    fills = data.get("fills") if isinstance(data.get("fills"), Mapping) else {}
    fill_model_id = str(
        fills.get("model_id", data.get("fill_model_id", "shadow_immediate_visible_depth_v0"))
    )
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
        entry_retry_cooldown_s=float(data.get("entry_retry_cooldown_s", 5.0)),
        entry_max_attempts=int(data.get("entry_max_attempts", 3)),
        exit_retry_cooldown_s=float(data.get("exit_retry_cooldown_s", 3.0)),
        exit_max_normal_retries=int(data.get("exit_max_normal_retries", 3)),
        exit_escalate_after=int(data.get("exit_escalate_after", 2)),
        fill_model_id=fill_model_id,
        fill_latency_ms=float(
            fills.get("latency_ms", data.get("fill_latency_ms", 0.0))
        ),
        fill_extra_slip_ticks=Decimal(
            str(fills.get("extra_slip_ticks", data.get("fill_extra_slip_ticks", "0")))
        ),
        fill_tick_size=Decimal(
            str(fills.get("tick_size", data.get("fill_tick_size", "0.01")))
        ),
        require_flat_for_promote=bool(data.get("require_flat_for_promote", True)),
    )
