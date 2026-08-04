"""Official ``polymarket-client`` SecureClient read-only oracle / transport.

Constructs ``SecureClient`` with existing env credentials — never create/derive.
Mutation methods are not exposed on this wrapper and raise if reached.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from tyrex_pm.adapters.polymarket.sdk_pagination import (
    SdkPaginationError,
    assert_not_sdk_page,
    map_sdk_paginator,
)
from tyrex_pm.adapters.polymarket.sdk_secure import (
    balance_allowance_to_decimal,
    build_secure_client,
)
from tyrex_pm.execution.polymarket.auth import (
    CredentialError,
    L2Credentials,
    load_l2_credentials,
    positions_wallet_address,
)
from tyrex_pm.execution.polymarket.normalize import venue_order_from_rest, venue_trade_from_rest
from tyrex_pm.execution.polymarket.transport import (
    VenueBalanceSnapshot,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
    VenueTradeSnapshot,
)

# Methods that must never be invoked during read-only / preflight.
_MUTATION_METHODS = frozenset(
    {
        "create_api_key",
        "derive_api_key",
        "create_or_derive_api_key",
        "create_and_post_order",
        "create_and_post_market_order",
        "create_order",
        "create_market_order",
        "create_limit_order",
        "place_market_order",
        "place_limit_order",
        "post_order",
        "post_orders",
        "cancel_order",
        "cancel_orders",
        "cancel_all",
        "cancel_market_orders",
        "post_heartbeat",
        "create_heartbeat_id",
        "update_balance_allowance",
        "delete_api_key",
        "create_builder_api_key",
    }
)


class MutationAttemptError(RuntimeError):
    pass


@dataclass
class NetworkAllowlistSpy:
    """Records host/path/method; rejects mutating paths."""

    allowed_methods: frozenset[str] = frozenset({"GET"})
    forbidden_path_substrings: tuple[str, ...] = (
        "/order",
        "/orders",
        "/heartbeats",
        "/auth/api-key",
        "/auth/derive-api-key",
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    def check(self, *, method: str, path: str) -> None:
        self.calls.append({"method": method.upper(), "path": path.split("?", 1)[0]})
        if method.upper() not in self.allowed_methods:
            raise MutationAttemptError(f"method not allowed: {method}")
        low = path.lower()
        if method.upper() != "GET":
            for frag in self.forbidden_path_substrings:
                if frag in low:
                    raise MutationAttemptError(f"mutating path blocked: {path}")
        if method.upper() == "GET" and "/heartbeats" in low:
            raise MutationAttemptError("heartbeat blocked")
        if "/auth/" in low:
            raise MutationAttemptError("credential create/derive blocked")


def build_official_readonly_client(
    *,
    env: dict[str, str] | None = None,
    creds: L2Credentials | None = None,
) -> Any:
    """Build official SecureClient with pre-existing API creds only (no derive)."""
    try:
        return build_secure_client(env=env, creds=creds)
    except CredentialError:
        raise
    except ImportError as exc:  # pragma: no cover
        raise CredentialError("polymarket-client not installed") from exc


def _open_order_to_snapshot(order: Any) -> VenueOrderSnapshot:
    assert_not_sdk_page(order, context="open_order_to_snapshot")
    if isinstance(order, dict):
        snap = venue_order_from_rest(order)
    else:
        order_id = getattr(order, "id", None)
        if order_id is None and not hasattr(order, "token_id"):
            raise SdkPaginationError(
                f"open_order_missing_identity:type={type(order).__name__}"
            )
        raw = {
            "id": order_id,
            "asset_id": str(getattr(order, "token_id", "") or ""),
            "market": str(
                getattr(order, "condition_id", None) or getattr(order, "market", "") or ""
            ),
            "side": getattr(order, "side", ""),
            "original_size": str(getattr(order, "original_size", "0")),
            "size_matched": str(getattr(order, "size_matched", "0")),
            "price": str(getattr(order, "price", "0")),
            "status": getattr(order, "status", ""),
        }
        snap = venue_order_from_rest(raw)
    if not snap.venue_order_id and not snap.instrument_token_id:
        raise SdkPaginationError("open_order_snapshot_empty_identity")
    return snap


def _trade_to_snapshot(trade: Any) -> VenueTradeSnapshot:
    assert_not_sdk_page(trade, context="trade_to_snapshot")
    if isinstance(trade, dict):
        snap = venue_trade_from_rest(trade)
    else:
        trade_id = getattr(trade, "id", None) or getattr(trade, "transaction_hash", None)
        if trade_id is None and not any(
            hasattr(trade, k) for k in ("asset", "token_id", "asset_id", "size")
        ):
            raise SdkPaginationError(
                f"trade_missing_identity:type={type(trade).__name__}"
            )
        raw = {
            "id": str(trade_id or ""),
            "taker_order_id": getattr(trade, "taker_order_id", None),
            "asset_id": str(
                getattr(trade, "asset", None)
                or getattr(trade, "token_id", None)
                or getattr(trade, "asset_id", "")
                or ""
            ),
            "side": getattr(trade, "side", ""),
            "size": str(getattr(trade, "size", "0")),
            "price": str(getattr(trade, "price", "0")),
            "status": getattr(trade, "status", "CONFIRMED"),
            "market": getattr(trade, "condition_id", getattr(trade, "market", None)),
        }
        snap = venue_trade_from_rest(raw)
    if not snap.venue_trade_id and not snap.instrument_token_id:
        raise SdkPaginationError("trade_snapshot_empty_identity")
    return snap


def _position_row_from_sdk(row: Any) -> dict[str, Any]:
    assert_not_sdk_page(row, context="position_row_from_sdk")
    if isinstance(row, dict):
        return row
    asset = str(getattr(row, "asset", getattr(row, "token_id", "")) or "")
    if not asset and not hasattr(row, "size"):
        raise SdkPaginationError(
            f"position_missing_identity:type={type(row).__name__}"
        )
    return {
        "asset": asset,
        "size": str(getattr(row, "size", "0")),
        "avgPrice": getattr(row, "avg_price", getattr(row, "avgPrice", None)),
        "conditionId": getattr(row, "condition_id", getattr(row, "conditionId", None)),
    }


@dataclass
class SdkReadonlyTransport:
    """Observation surface over official SecureClient — no mutation methods."""

    _client: Any
    creds: L2Credentials
    spy: NetworkAllowlistSpy = field(default_factory=NetworkAllowlistSpy)
    _handlers: list[Callable] = field(default_factory=list)
    last_pagination: dict[str, dict[str, int]] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> SdkReadonlyTransport:
        source = env if env is not None else dict(os.environ)
        creds = load_l2_credentials(source)
        client = build_official_readonly_client(env=source, creds=creds)
        return cls(_client=client, creds=creds)

    def __getattribute__(self, name: str) -> Any:
        if name in _MUTATION_METHODS:
            raise MutationAttemptError(f"SdkReadonlyTransport forbids {name}")
        return super().__getattribute__(name)

    def _record_pagination(self, key: str, *, page_count: int, record_count: int) -> None:
        self.last_pagination[key] = {
            "page_count": int(page_count),
            "record_count": int(record_count),
        }

    def get_open_orders(self, *, market_id: str | None = None) -> list[VenueOrderSnapshot]:
        self.spy.check(method="GET", path="/data/orders")
        kwargs: dict[str, Any] = {}
        if market_id:
            kwargs["market"] = market_id
        paginator = self._client.list_open_orders(**kwargs)
        rows, drained = map_sdk_paginator(paginator, _open_order_to_snapshot)
        self._record_pagination(
            "open_orders",
            page_count=drained.page_count,
            record_count=drained.record_count,
        )
        return rows

    def get_order(self, venue_order_id: str) -> VenueOrderSnapshot | None:
        self.spy.check(method="GET", path="/data/order")
        try:
            raw = self._client.get_order(order_id=venue_order_id)
        except Exception:  # noqa: BLE001
            return None
        if raw is None:
            return None
        return _open_order_to_snapshot(raw)

    def get_trades(
        self, *, market_id: str | None = None, after: str | None = None
    ) -> list[VenueTradeSnapshot]:
        _ = after
        self.spy.check(method="GET", path="/data/trades")
        kwargs: dict[str, Any] = {}
        if market_id:
            kwargs["market"] = [market_id]
        # Fail closed on pagination/conversion errors (do not invent empty success).
        paginator = self._client.list_trades(**kwargs)
        out, drained = map_sdk_paginator(paginator, _trade_to_snapshot)
        self._record_pagination(
            "trades",
            page_count=drained.page_count,
            record_count=drained.record_count,
        )
        if market_id:
            out = [t for t in out if t.market_id == market_id]
        return out

    def get_balance(self) -> VenueBalanceSnapshot:
        self.spy.check(method="GET", path="/balance-allowance")
        raw = self._client.get_balance_allowance(asset_type="COLLATERAL")
        bal, allowance = balance_allowance_to_decimal(raw, conditional=False)
        return VenueBalanceSnapshot(collateral_balance=bal, allowance=allowance)

    def get_conditional_balance_allowance(
        self, token_id: str
    ) -> tuple[Decimal, Decimal | None]:
        """CONDITIONAL balance/allowance for the funder/proxy owner (share units)."""
        from tyrex_pm.execution.polymarket.address_roles import roles_from_credentials

        roles = roles_from_credentials(self.creds)
        roles.assert_conditional_query_target(queried_as=roles.conditional_owner)
        if roles.proxy_mode and roles.conditional_owner.lower() == roles.signer_eoa.lower():
            if roles.funder_proxy and roles.funder_proxy.lower() != roles.signer_eoa.lower():
                from tyrex_pm.execution.polymarket.address_roles import AddressRoleError

                raise AddressRoleError("REFUSING_SIGNER_CONDITIONAL_BALANCE_WHEN_PROXY_MODE")

        self.spy.check(method="GET", path="/balance-allowance")
        raw = self._client.get_balance_allowance(
            asset_type="CONDITIONAL", token_id=token_id
        )
        return balance_allowance_to_decimal(raw, conditional=True)

    def get_positions_raw(self) -> list[dict[str, Any]]:
        """Full Data API position rows for the funder/proxy (ack validation)."""
        user = positions_wallet_address(self.creds)
        self.spy.check(method="GET", path="/positions")
        paginator = self._client.list_positions(user=user)
        rows, drained = map_sdk_paginator(paginator, _position_row_from_sdk)
        self._record_pagination(
            "positions",
            page_count=drained.page_count,
            record_count=drained.record_count,
        )
        return rows

    def get_positions(self) -> list[VenuePositionSnapshot]:
        out: list[VenuePositionSnapshot] = []
        for r in self.get_positions_raw():
            asset = str(r.get("asset") or r.get("token_id") or "")
            if not asset:
                continue
            out.append(
                VenuePositionSnapshot(
                    instrument_token_id=asset,
                    size=Decimal(str(r.get("size") or "0")),
                    avg_price=(
                        None if r.get("avgPrice") is None else Decimal(str(r.get("avgPrice")))
                    ),
                    market_id=(
                        None if r.get("conditionId") is None else str(r.get("conditionId"))
                    ),
                )
            )
        return out

    def get_server_time(self) -> int | None:
        """Delegate to the single narrow official ``/time`` adapter."""
        self.spy.check(method="GET", path="/time")
        try:
            from tyrex_pm.adapters.polymarket.clob_server_time import fetch_clob_server_time

            return int(fetch_clob_server_time().unix_seconds)
        except Exception:  # noqa: BLE001
            return None

    def subscribe_user_events(self, handler: Callable) -> None:
        self._handlers.append(handler)

    def stop(self) -> None:
        self._handlers.clear()
        close = getattr(self._client, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
