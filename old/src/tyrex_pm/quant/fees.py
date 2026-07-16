"""Dynamic Polymarket taker fee curve for Z-Gap edge math (A0.4).

Uses ``fd`` from ``/clob-markets/<condition_id>`` (``MarketInfo.raw``).

Do **not** use ``fee_rate_bps`` / ``/fee-rate`` ``base_fee`` for Z-Gap edge.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

FEE_MODEL_ID_DYNAMIC_FD_V1 = "polymarket_dynamic_fd_v1"
FEE_MODEL_STATUS_RESOLVED = "resolved"
FEE_MODEL_STATUS_UNKNOWN = "unknown"
FEE_SOURCE_CLOB_MARKETS_FD = "clob_markets_fd"


@dataclass(frozen=True)
class FeeModel:
    """Parsed dynamic fee descriptor for Z-Gap φ(price) edge math."""

    fee_model_id: str
    fee_model_status: str
    fd_r: Decimal | None
    fd_e: Decimal | None
    fd_to: bool | None
    condition_id: str | None = None
    market_id: str | None = None
    source: str = FEE_SOURCE_CLOB_MARKETS_FD

    @property
    def is_resolved(self) -> bool:
        return self.fee_model_status == FEE_MODEL_STATUS_RESOLVED


def _decimal_param(raw: object) -> Decimal | None:
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def validate_fee_price(price: Decimal) -> None:
    """Reject prices outside binary probability scale [0, 1]."""
    if price < 0 or price > 1:
        raise ValueError(f"fee price must be in [0, 1], got {price}")


def curve_parameters_hash(
    *,
    fd_r: Decimal | None,
    fd_e: Decimal | None,
    fd_to: bool | None,
) -> str | None:
    if fd_r is None or fd_e is None:
        return None
    payload = {
        "r": str(fd_r),
        "e": str(fd_e),
        "to": fd_to,
        "fee_model_id": FEE_MODEL_ID_DYNAMIC_FD_V1,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:16]


def parse_fee_model_from_raw(
    raw: dict[str, Any] | None,
    *,
    condition_id: str | None = None,
    market_id: str | None = None,
    fee_rate_bps: int | None = None,
) -> FeeModel:
    """Parse ``fd`` from a ``/clob-markets`` raw payload.

    ``fee_rate_bps`` (from ``/fee-rate``) is intentionally ignored for Z-Gap edge.
    """
    _ = fee_rate_bps  # documented: flat bps is not used for dynamic φ edge math.
    if not raw or not isinstance(raw, dict):
        return FeeModel(
            fee_model_id=FEE_MODEL_ID_DYNAMIC_FD_V1,
            fee_model_status=FEE_MODEL_STATUS_UNKNOWN,
            fd_r=None,
            fd_e=None,
            fd_to=None,
            condition_id=condition_id,
            market_id=market_id,
        )

    fd = raw.get("fd")
    if not isinstance(fd, dict):
        return FeeModel(
            fee_model_id=FEE_MODEL_ID_DYNAMIC_FD_V1,
            fee_model_status=FEE_MODEL_STATUS_UNKNOWN,
            fd_r=None,
            fd_e=None,
            fd_to=None,
            condition_id=condition_id or str(raw.get("c") or raw.get("condition_id") or "") or None,
            market_id=market_id,
        )

    fd_r = _decimal_param(fd.get("r"))
    fd_e = _decimal_param(fd.get("e"))
    fd_to_raw = fd.get("to")
    fd_to = bool(fd_to_raw) if fd_to_raw is not None else None

    if fd_r is None or fd_e is None:
        return FeeModel(
            fee_model_id=FEE_MODEL_ID_DYNAMIC_FD_V1,
            fee_model_status=FEE_MODEL_STATUS_UNKNOWN,
            fd_r=fd_r,
            fd_e=fd_e,
            fd_to=fd_to,
            condition_id=condition_id or str(raw.get("c") or "") or None,
            market_id=market_id,
        )

    return FeeModel(
        fee_model_id=FEE_MODEL_ID_DYNAMIC_FD_V1,
        fee_model_status=FEE_MODEL_STATUS_RESOLVED,
        fd_r=fd_r,
        fd_e=fd_e,
        fd_to=fd_to,
        condition_id=condition_id or str(raw.get("c") or "") or None,
        market_id=market_id,
    )


def parse_fee_model_from_market_info(
    market_info: Any,
    *,
    market_id: str | None = None,
) -> FeeModel:
    """Parse dynamic fee model from :class:`tyrex_pm.venue.polymarket.market_info.MarketInfo`."""
    raw = getattr(market_info, "raw", None)
    condition_id = getattr(market_info, "condition_id", None)
    fee_rate_bps = getattr(market_info, "fee_rate_bps", None)
    if not isinstance(raw, dict):
        raw = None
    return parse_fee_model_from_raw(
        raw,
        condition_id=str(condition_id) if condition_id else None,
        market_id=market_id,
        fee_rate_bps=int(fee_rate_bps) if fee_rate_bps is not None else None,
    )


def phi_taker_fee(price: Decimal, fee_model: FeeModel) -> Decimal:
    """Dynamic taker fee per share: ``fd.r * (price * (1 - price)) ** fd.e``.

    Price must be in [0, 1] (probability units, not cents).
    """
    if not fee_model.is_resolved:
        raise ValueError("fee model is not resolved")
    if fee_model.fd_r is None or fee_model.fd_e is None:
        raise ValueError("fee model missing fd parameters")

    validate_fee_price(price)
    if price == 0 or price == 1:
        return Decimal("0")

    one = Decimal("1")
    base = price * (one - price)
    # Integer exponents use exact Decimal power; non-integer uses float bridge.
    exponent = fee_model.fd_e
    if exponent == exponent.to_integral_value():
        fee = fee_model.fd_r * base ** int(exponent)
    else:
        fee = fee_model.fd_r * (Decimal(str(float(base) ** float(exponent))))
    return fee


def flat_fee_from_bps(price: Decimal, fee_rate_bps: int) -> Decimal:
    """Flat ``/fee-rate`` style fee — **not** for Z-Gap edge math.

    Exposed for tests/documentation only.
    """
    validate_fee_price(price)
    return price * Decimal(fee_rate_bps) / Decimal("10000")
