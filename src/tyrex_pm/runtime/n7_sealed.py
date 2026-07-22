"""N7 sealed production configuration (defaults OFF; Scope A only)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from tyrex_pm.runtime.live_config import LiveConfig, LiveScope, live_config_from_mapping
from tyrex_pm.runtime.n7_timing import (
    ACKNOWLEDGMENT_TIMEOUT_S,
    ACK_TIMEOUT_MS,
    CANCEL_RECON_BUDGET_S,
    DISCRETIONARY_EXIT_CUTOFF_BEFORE_END_S,
    EVENT_END_SAFETY_BUFFER_S,
    EXIT_RETRY_MAX_ATTEMPTS,
    EXIT_RETRY_TIME_BUDGET_MS,
    LAST_ALLOWED_ENTRY_BEFORE_END_S,
    MANDATORY_FLATTEN_START_BEFORE_END_S,
    N7_TIMING,
    N7TimingFreeze,
    PRODUCTION_TIMING_VALUES_STATUS,
    RESIDUAL_OPERATOR_DEADLINE_BEFORE_END_S,
)


@dataclass(frozen=True, kw_only=True)
class N7SealedConfig:
    """Complete N7 one-shot sealed config.

    Defaults keep mutations OFF. Cap is a hard maximum ($5), never a target.
    Daily notional accounts entry exposure only — inventory-reducing exits do
    not consume additional daily notional.
    """

    live: LiveConfig
    timing: N7TimingFreeze = N7_TIMING
    max_buy_collateral: Decimal = Decimal("5.00")
    max_daily_notional: Decimal = Decimal("5.00")
    max_daily_loss: Decimal = Decimal("5.00")
    max_positions_per_window: int = 1
    max_entry_lineages: int = 1
    allow_same_window_reentry: bool = False
    allow_same_window_reversal: bool = False
    skip_if_min_exceeds_cap: bool = True
    order_style: str = "marketable_limit"
    resolution_capability: bool = False
    market_family: str = "btc_updown_5m"
    one_shot: bool = True
    # When false: sealed Chainlink K is PTB-ready; SSR openPrice is not fetched/gated.
    require_ssr_price_match: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.require_ssr_price_match, bool):
            raise ValueError("require_ssr_price_match must be a boolean")
        if self.live.scope is not LiveScope.A:
            raise ValueError("N7 requires live.scope=A")
        if self.max_buy_collateral > Decimal("5.00"):
            raise ValueError("max_buy_collateral must be <= 5.00 USDC")
        if self.live.hard_collateral_cap is not None and self.live.hard_collateral_cap > Decimal(
            "5.00"
        ):
            raise ValueError("live.hard_collateral_cap must be <= 5.00 USDC")
        if self.max_entry_lineages != 1:
            raise ValueError("N7 allows exactly one entry lineage")
        if self.max_positions_per_window != 1:
            raise ValueError("N7 allows exactly one position per window")
        if self.allow_same_window_reentry or self.allow_same_window_reversal:
            raise ValueError("N7 forbids same-window re-entry/reversal")
        if self.resolution_capability:
            raise ValueError("N7 requires z_gap.resolution_capability=false")
        if not self.skip_if_min_exceeds_cap:
            raise ValueError("N7 requires skip_if_min_exceeds_cap=true")
        if not self.one_shot:
            raise ValueError("N7A/N7B require one_shot=true")

    def fingerprint(self) -> str:
        payload = {
            "live": {
                "enabled": self.live.enabled,
                "mutations_enabled": self.live.mutations_enabled,
                "scope": self.live.scope.value,
                "ack_timeout_ms": self.live.ack_timeout_ms,
                "max_order_notional": (
                    None
                    if self.live.max_order_notional is None
                    else str(self.live.max_order_notional)
                ),
                "order_style": self.live.order_style,
                "hard_collateral_cap": (
                    None
                    if self.live.hard_collateral_cap is None
                    else str(self.live.hard_collateral_cap)
                ),
            },
            "timing": self.timing.fingerprint_payload(),
            "max_buy_collateral": str(self.max_buy_collateral),
            "max_daily_notional": str(self.max_daily_notional),
            "max_daily_loss": str(self.max_daily_loss),
            "max_positions_per_window": self.max_positions_per_window,
            "max_entry_lineages": self.max_entry_lineages,
            "allow_same_window_reentry": self.allow_same_window_reentry,
            "allow_same_window_reversal": self.allow_same_window_reversal,
            "skip_if_min_exceeds_cap": self.skip_if_min_exceeds_cap,
            "order_style": self.order_style,
            "resolution_capability": self.resolution_capability,
            "market_family": self.market_family,
            "one_shot": self.one_shot,
            "require_ssr_price_match": self.require_ssr_price_match,
            "production_timing_status": PRODUCTION_TIMING_VALUES_STATUS,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "live": self.live.to_dict(),
            "timing": self.timing.fingerprint_payload(),
            "max_buy_collateral": str(self.max_buy_collateral),
            "max_daily_notional": str(self.max_daily_notional),
            "max_daily_loss": str(self.max_daily_loss),
            "max_positions_per_window": self.max_positions_per_window,
            "max_entry_lineages": self.max_entry_lineages,
            "skip_if_min_exceeds_cap": self.skip_if_min_exceeds_cap,
            "order_style": self.order_style,
            "resolution_capability": self.resolution_capability,
            "market_family": self.market_family,
            "one_shot": self.one_shot,
            "require_ssr_price_match": self.require_ssr_price_match,
            "ssr_match_required": self.require_ssr_price_match,
            "ptb_authority": (
                "chainlink_sealed_k_and_ssr_match"
                if self.require_ssr_price_match
                else "chainlink_sealed_k"
            ),
            "fingerprint": self.fingerprint(),
            "scope_b_available": False,
            "hold_to_resolution_available": False,
            "redeem_available": False,
            "mutations_default": False,
        }


def default_n7_sealed_config() -> N7SealedConfig:
    """Sealed defaults with mutations OFF."""
    live = LiveConfig(
        enabled=False,
        mutations_enabled=False,
        scope=LiveScope.A,
        ack_timeout_ms=N7_TIMING.ack_timeout_ms,
        max_order_notional=Decimal("5.00"),
        order_style="marketable_limit",
        hard_collateral_cap=Decimal("5.00"),
    )
    return N7SealedConfig(live=live)


def n7_sealed_from_mapping(raw: Mapping[str, Any]) -> N7SealedConfig:
    live_raw = dict(raw.get("live") or {})
    max_buy = Decimal(
        str(raw.get("max_buy_collateral", live_raw.get("max_buy_collateral", "5.00")))
    )
    if "hard_collateral_cap" not in live_raw:
        live_raw["hard_collateral_cap"] = str(max_buy)
    if "max_order_notional" not in live_raw:
        live_raw["max_order_notional"] = str(max_buy)
    if "ack_timeout_ms" not in live_raw:
        live_raw["ack_timeout_ms"] = N7_TIMING.ack_timeout_ms
    if "order_style" not in live_raw:
        live_raw["order_style"] = str(raw.get("order_style", "marketable_limit"))
    # Defaults OFF unless file explicitly sets them.
    live_raw.setdefault("enabled", False)
    live_raw.setdefault("mutations_enabled", False)
    live = live_config_from_mapping(live_raw)
    z_gap = dict(raw.get("z_gap") or {})
    timing_raw = dict(raw.get("timing") or {})
    timing = N7TimingFreeze(
        last_allowed_entry_before_end_s=float(
            timing_raw.get(
                "last_allowed_entry_before_end_s", LAST_ALLOWED_ENTRY_BEFORE_END_S
            )
        ),
        discretionary_exit_cutoff_before_end_s=float(
            timing_raw.get(
                "discretionary_exit_cutoff_before_end_s",
                DISCRETIONARY_EXIT_CUTOFF_BEFORE_END_S,
            )
        ),
        mandatory_flatten_start_before_end_s=float(
            timing_raw.get(
                "mandatory_flatten_start_before_end_s",
                MANDATORY_FLATTEN_START_BEFORE_END_S,
            )
        ),
        residual_operator_deadline_before_end_s=float(
            timing_raw.get(
                "residual_operator_deadline_before_end_s",
                RESIDUAL_OPERATOR_DEADLINE_BEFORE_END_S,
            )
        ),
        event_end_safety_buffer_s=float(
            timing_raw.get("event_end_safety_buffer_s", EVENT_END_SAFETY_BUFFER_S)
        ),
        acknowledgment_timeout_s=float(
            timing_raw.get("acknowledgment_timeout_s", ACKNOWLEDGMENT_TIMEOUT_S)
        ),
        cancel_recon_budget_s=float(
            timing_raw.get("cancel_recon_budget_s", CANCEL_RECON_BUDGET_S)
        ),
        exit_retry_max_attempts=int(
            timing_raw.get("exit_retry_max_attempts", EXIT_RETRY_MAX_ATTEMPTS)
        ),
        exit_retry_time_budget_ms=int(
            timing_raw.get("exit_retry_time_budget_ms", EXIT_RETRY_TIME_BUDGET_MS)
        ),
        ack_timeout_ms=int(timing_raw.get("ack_timeout_ms", ACK_TIMEOUT_MS)),
    )
    return N7SealedConfig(
        live=live,
        timing=timing,
        max_buy_collateral=max_buy,
        max_daily_notional=Decimal(str(raw.get("max_daily_notional", "5.00"))),
        max_daily_loss=Decimal(str(raw.get("max_daily_loss", "5.00"))),
        max_positions_per_window=int(raw.get("max_positions_per_window", 1)),
        max_entry_lineages=int(raw.get("max_entry_lineages", 1)),
        allow_same_window_reentry=bool(raw.get("allow_same_window_reentry", False)),
        allow_same_window_reversal=bool(raw.get("allow_same_window_reversal", False)),
        skip_if_min_exceeds_cap=bool(raw.get("skip_if_min_exceeds_cap", True)),
        order_style=str(raw.get("order_style", live.order_style)),
        resolution_capability=bool(z_gap.get("resolution_capability", False)),
        market_family=str(raw.get("market_family", "btc_updown_5m")),
        one_shot=bool(raw.get("one_shot", True)),
        require_ssr_price_match=_as_bool(
            raw.get("require_ssr_price_match", False),
            field="require_ssr_price_match",
        ),
    )


def _as_bool(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{field} must be a boolean, got {type(value).__name__}")


def load_n7_sealed_config(path: Path) -> N7SealedConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("N7 config root must be a mapping")
    return n7_sealed_from_mapping(raw)
