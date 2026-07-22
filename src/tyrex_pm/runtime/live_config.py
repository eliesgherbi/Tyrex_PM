"""N6 live runtime configuration — mutations default OFF, Scope A only."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping


class LiveScope(str, Enum):
    """Live monetization scope.

    Scope B (hold-to-resolution / redeem) is enumerated for refusal only —
    it is not implemented or silently enabled by N6.
    """

    A = "A"
    B = "B"


@dataclass(frozen=True, kw_only=True)
class LiveConfig:
    """Generic live execution flags (strategy-independent).

    Defaults fail closed: ``enabled=False``, ``mutations_enabled=False``,
    ``scope=A``. Scope B cannot be activated by this config alone — redeem /
    finality ports remain absent.
    """

    enabled: bool = False
    mutations_enabled: bool = False
    scope: LiveScope = LiveScope.A
    ack_timeout_ms: int | None = None  # OPEN for production; tests supply explicit values
    max_order_notional: Decimal | None = None
    order_style: str = "marketable_limit"
    # Fee-inclusive hard collateral cap (USDC). SKIP if min valid order exceeds.
    hard_collateral_cap: Decimal | None = None

    def __post_init__(self) -> None:
        if self.ack_timeout_ms is not None and self.ack_timeout_ms <= 0:
            raise ValueError("ack_timeout_ms must be > 0 when set")
        if self.max_order_notional is not None and self.max_order_notional <= 0:
            raise ValueError("max_order_notional must be > 0 when set")
        if self.hard_collateral_cap is not None and self.hard_collateral_cap <= 0:
            raise ValueError("hard_collateral_cap must be > 0 when set")
        if self.mutations_enabled and not self.enabled:
            raise ValueError("live.mutations_enabled requires live.enabled=true")
        if self.scope is LiveScope.B:
            # Fail closed: Scope B is not available in N6.
            raise ValueError(
                "live.scope=B is structurally unsupported in N6 "
                "(redeem/finality not implemented); use scope=A"
            )

    def fingerprint(self) -> str:
        payload = {
            "enabled": self.enabled,
            "mutations_enabled": self.mutations_enabled,
            "scope": self.scope.value,
            "ack_timeout_ms": self.ack_timeout_ms,
            "max_order_notional": (
                None if self.max_order_notional is None else str(self.max_order_notional)
            ),
            "order_style": self.order_style,
            "hard_collateral_cap": (
                None
                if self.hard_collateral_cap is None
                else str(self.hard_collateral_cap)
            ),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mutations_enabled": self.mutations_enabled,
            "scope": self.scope.value,
            "live_scope": self.scope.value,
            "ack_timeout_ms": self.ack_timeout_ms,
            "max_order_notional": (
                None if self.max_order_notional is None else str(self.max_order_notional)
            ),
            "order_style": self.order_style,
            "hard_collateral_cap": (
                None
                if self.hard_collateral_cap is None
                else str(self.hard_collateral_cap)
            ),
            "fingerprint": self.fingerprint(),
            "scope_b_available": False,
            "hold_to_resolution_available": False,
            "redeem_available": False,
        }


def live_config_from_mapping(raw: Mapping[str, Any] | None) -> LiveConfig:
    """Parse optional ``live`` block. Missing block → all defaults (OFF)."""
    if raw is None:
        return LiveConfig()
    if not isinstance(raw, Mapping):
        raise ValueError("live config must be a mapping")
    scope_raw = str(raw.get("scope", "A")).upper()
    try:
        scope = LiveScope(scope_raw)
    except ValueError as exc:
        raise ValueError(f"live.scope must be A or B, got {scope_raw!r}") from exc
    max_n = raw.get("max_order_notional")
    hard = raw.get("hard_collateral_cap")
    return LiveConfig(
        enabled=bool(raw.get("enabled", False)),
        mutations_enabled=bool(raw.get("mutations_enabled", False)),
        scope=scope,
        ack_timeout_ms=(
            None if raw.get("ack_timeout_ms") is None else int(raw["ack_timeout_ms"])
        ),
        max_order_notional=None if max_n is None else Decimal(str(max_n)),
        order_style=str(raw.get("order_style", "marketable_limit")),
        hard_collateral_cap=None if hard is None else Decimal(str(hard)),
    )
