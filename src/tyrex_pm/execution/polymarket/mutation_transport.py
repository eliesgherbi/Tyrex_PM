"""Mutation transports: spy/fake for R7A; gated official polymarket-client wrapper."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from tyrex_pm.execution.polymarket.transport import (
    CancelOrderResult,
    SubmitOrderRequest,
    SubmitOrderResult,
)


class MutationGateError(RuntimeError):
    pass


@dataclass
class MutationArmToken:
    """Opaque arm token. Network mutations require allow_network=True.

    R7A never issues a live network arm token. R7B must obtain one only after
    second user authorization matching the approval artifact.
    """

    artifact_id: str
    allow_network: bool = False


@dataclass
class SpyMutationTransport:
    """Records submit/cancel; never reaches the network."""

    submitted: list[SubmitOrderRequest] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    submit_behavior: str = "accept"  # accept|reject|timeout
    cancel_behavior: str = "accept"
    _submit_n: int = 0

    def submit_order(self, request: SubmitOrderRequest) -> SubmitOrderResult:
        self._submit_n += 1
        self.submitted.append(request)
        if self.submit_behavior == "reject":
            return SubmitOrderResult(
                ok=False, venue_order_id=None, status=None, error="REJECTED"
            )
        if self.submit_behavior == "timeout":
            return SubmitOrderResult(
                ok=False,
                venue_order_id=None,
                status=None,
                error="timeout",
                uncertain=True,
            )
        vid = f"0xspy{self._submit_n:04d}"
        return SubmitOrderResult(ok=True, venue_order_id=vid, status="matched")

    def cancel_order(self, venue_order_id: str) -> CancelOrderResult:
        if venue_order_id.startswith("0xexternal"):
            raise MutationGateError("REFUSING_UNKNOWN_EXTERNAL_CANCEL")
        self.cancelled.append(venue_order_id)
        if self.cancel_behavior == "timeout":
            return CancelOrderResult(
                ok=False, venue_order_id=venue_order_id, uncertain=True, error="timeout"
            )
        return CancelOrderResult(ok=True, venue_order_id=venue_order_id)

    def cancel_all(self) -> None:  # pragma: no cover - structural forbid
        raise MutationGateError("CANCEL_ALL_FORBIDDEN")


def _as_decimal(value: str | None, fallback: str) -> Decimal:
    return Decimal(str(value if value is not None else fallback))


def _submit_result_from_sdk(resp: Any) -> SubmitOrderResult:
    if isinstance(resp, dict):
        success = bool(resp.get("success", resp.get("ok", True)))
        oid = resp.get("orderID") or resp.get("order_id")
        status = resp.get("status")
        if not success:
            return SubmitOrderResult(
                ok=False,
                venue_order_id=str(oid) if oid else None,
                status=str(status) if status else None,
                error=str(resp.get("errorMsg") or resp.get("message") or "REJECTED"),
                raw=resp,
            )
        return SubmitOrderResult(
            ok=True,
            venue_order_id=str(oid) if oid else None,
            status=str(status) if status else None,
            raw=resp,
        )
    ok = bool(getattr(resp, "ok", True))
    if not ok:
        return SubmitOrderResult(
            ok=False,
            venue_order_id=None,
            status=None,
            error=str(getattr(resp, "message", None) or getattr(resp, "code", "REJECTED")),
            raw={"code": getattr(resp, "code", None), "message": getattr(resp, "message", None)},
        )
    oid = getattr(resp, "order_id", None)
    status = getattr(resp, "status", None)
    return SubmitOrderResult(
        ok=True,
        venue_order_id=str(oid) if oid else None,
        status=str(status) if status else None,
        raw={
            "order_id": oid,
            "status": status,
            "making_amount": str(getattr(resp, "making_amount", "")),
            "taking_amount": str(getattr(resp, "taking_amount", "")),
        },
    )


@dataclass
class SdkMutationTransport:
    """Official ``polymarket-client`` SecureClient mutation wrapper.

    Tyrex owns OMS/budget/lifecycle. The SDK only constructs, signs, posts, and
    cancels. Network calls require an arm token with ``allow_network=True``.
    Heartbeat is never started by this transport unless explicitly enabled.
    """

    _client: Any
    arm: MutationArmToken | None = None
    heartbeat_enabled: bool = False

    def enable_network(self, arm: MutationArmToken) -> None:
        if not arm.allow_network:
            raise MutationGateError("ARM_TOKEN_DISALLOWS_NETWORK")
        self.arm = arm

    def disable_network(self) -> None:
        self.arm = None

    def _require_armed(self) -> None:
        if self.arm is None or not self.arm.allow_network:
            raise MutationGateError("NETWORK_MUTATION_NOT_ARMED")

    def submit_order(self, request: SubmitOrderRequest) -> SubmitOrderResult:
        self._require_armed()
        side = request.side.upper()
        if side not in {"BUY", "SELL"}:
            return SubmitOrderResult(
                ok=False, venue_order_id=None, status=None, error="BAD_SIDE"
            )
        ot = request.order_type.upper()
        try:
            if ot in {"FAK", "FOK"}:
                price = _as_decimal(request.price, "0")
                if side == "BUY":
                    # BUY market: amount = USDC notional to spend; max_price caps.
                    amount = _as_decimal(request.amount or request.size, "0")
                    resp = self._client.place_market_order(
                        token_id=request.token_id,
                        side="BUY",
                        amount=amount,
                        max_price=price,
                        order_type=ot,  # type: ignore[arg-type]
                    )
                else:
                    # SELL market: shares to sell.
                    shares = _as_decimal(request.amount or request.size, "0")
                    resp = self._client.place_market_order(
                        token_id=request.token_id,
                        side="SELL",
                        shares=shares,
                        min_price=price,
                        order_type=ot,  # type: ignore[arg-type]
                    )
            else:
                resp = self._client.place_limit_order(
                    token_id=request.token_id,
                    price=_as_decimal(request.price, "0"),
                    size=_as_decimal(request.size, "0"),
                    side=side,  # type: ignore[arg-type]
                )
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            uncertain = "timeout" in err.lower() or "connection" in err.lower()
            return SubmitOrderResult(
                ok=False,
                venue_order_id=None,
                status=None,
                error=err,
                uncertain=uncertain,
            )
        return _submit_result_from_sdk(resp)

    def cancel_order(self, venue_order_id: str) -> CancelOrderResult:
        self._require_armed()
        if not venue_order_id or venue_order_id.startswith("0xexternal"):
            raise MutationGateError("REFUSING_UNKNOWN_EXTERNAL_CANCEL")
        try:
            resp = self._client.cancel_order(order_id=venue_order_id)
        except TypeError:
            # Deterministic test fakes may use positional cancel_order(oid).
            try:
                resp = self._client.cancel_order(venue_order_id)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                uncertain = "timeout" in err.lower() or "connection" in err.lower()
                return CancelOrderResult(
                    ok=False,
                    venue_order_id=venue_order_id,
                    error=err,
                    uncertain=uncertain,
                )
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            uncertain = "timeout" in err.lower() or "connection" in err.lower()
            return CancelOrderResult(
                ok=False,
                venue_order_id=venue_order_id,
                error=err,
                uncertain=uncertain,
            )
        raw: dict[str, Any]
        if isinstance(resp, dict):
            raw = resp
        else:
            raw = {"canceled": getattr(resp, "canceled", None), "raw": str(resp)}
        return CancelOrderResult(ok=True, venue_order_id=venue_order_id, raw=raw)

    def cancel_all(self) -> None:
        raise MutationGateError("CANCEL_ALL_FORBIDDEN")
