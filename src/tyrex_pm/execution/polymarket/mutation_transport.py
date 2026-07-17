"""Mutation transports: spy/fake for R7A; gated SDK wrapper for future R7B."""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass
class SdkMutationTransport:
    """Official ``py-clob-client-v2`` mutation wrapper.

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
        # Heartbeat is never auto-started here. R7A/R7B FAK one-shot keeps
        # heartbeat_enabled=False so POST /heartbeats is never invoked.

    def submit_order(self, request: SubmitOrderRequest) -> SubmitOrderResult:
        self._require_armed()
        try:
            from py_clob_client_v2 import (
                MarketOrderArgs,
                OrderArgs,
                OrderType,
                PartialCreateOrderOptions,
            )
            from py_clob_client_v2.order_builder.constants import BUY, SELL
        except ImportError as exc:  # pragma: no cover
            raise MutationGateError("SDK_NOT_INSTALLED") from exc

        side = BUY if request.side.upper() == "BUY" else SELL
        tick = request.tick_size or "0.01"
        options = PartialCreateOrderOptions(tick_size=tick, neg_risk=request.neg_risk)
        ot = getattr(OrderType, request.order_type.upper(), None)
        if ot is None:
            return SubmitOrderResult(
                ok=False, venue_order_id=None, status=None, error="BAD_ORDER_TYPE"
            )

        try:
            if request.order_type.upper() in {"FAK", "FOK"}:
                # Official market order: BUY amount=dollars; SELL amount=shares
                if request.side.upper() == "BUY":
                    amount = float(request.amount or request.size)
                else:
                    amount = float(request.amount or request.size)
                resp = self._client.create_and_post_market_order(
                    order_args=MarketOrderArgs(
                        token_id=request.token_id,
                        side=side,
                        amount=amount,
                        price=float(request.price),
                        order_type=ot,
                    ),
                    options=options,
                    order_type=ot,
                )
            else:
                resp = self._client.create_and_post_order(
                    OrderArgs(
                        token_id=request.token_id,
                        price=float(request.price),
                        size=float(request.size),
                        side=side,
                    ),
                    options=options,
                    order_type=ot,
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

        if not isinstance(resp, dict):
            resp = {"raw": resp}
        success = bool(resp.get("success", True))
        oid = resp.get("orderID") or resp.get("order_id")
        status = resp.get("status")
        if not success:
            return SubmitOrderResult(
                ok=False,
                venue_order_id=str(oid) if oid else None,
                status=str(status) if status else None,
                error=str(resp.get("errorMsg") or "REJECTED"),
                raw=resp,
            )
        return SubmitOrderResult(
            ok=True,
            venue_order_id=str(oid) if oid else None,
            status=str(status) if status else None,
            raw=resp,
        )

    def cancel_order(self, venue_order_id: str) -> CancelOrderResult:
        self._require_armed()
        if not venue_order_id or venue_order_id.startswith("0xexternal"):
            raise MutationGateError("REFUSING_UNKNOWN_EXTERNAL_CANCEL")
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
        if not isinstance(resp, dict):
            resp = {"raw": resp}
        return CancelOrderResult(
            ok=True, venue_order_id=venue_order_id, raw=resp
        )

    def cancel_all(self) -> None:
        raise MutationGateError("CANCEL_ALL_FORBIDDEN")
