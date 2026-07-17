"""Official ``py-clob-client-v2`` read-only oracle / transport (R6D Option A).

Constructs ``ClobClient`` with existing env credentials — never create/derive.
Mutation methods are not exposed on this wrapper and raise if reached.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import urlencode

from tyrex_pm.execution.polymarket.auth import (
    CredentialError,
    L2Credentials,
    _funder_from_env,
    _private_key_from_env,
    _signature_type_from_env,
    _strip_env,
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

CLOB_HOST = "https://clob.polymarket.com"

# Methods that must never be invoked during R6D / preflight.
_MUTATION_METHODS = frozenset(
    {
        "create_api_key",
        "derive_api_key",
        "create_or_derive_api_key",
        "create_and_post_order",
        "create_and_post_market_order",
        "create_order",
        "create_market_order",
        "post_order",
        "post_orders",
        "cancel_order",
        "cancel_orders",
        "cancel_market_orders",
        "post_heartbeat",
        "create_heartbeat_id",
        "update_balance_allowance",
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
        # Allow /data/orders (GET) but not POST /order
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
    """Build official ClobClient with pre-existing API creds only (no derive)."""
    try:
        from py_clob_client_v2 import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds
    except ImportError as exc:
        raise CredentialError("py-clob-client-v2 not installed") from exc

    source = env if env is not None else dict(os.environ)
    loaded = creds or load_l2_credentials(source)
    pk = _private_key_from_env(source)
    if not pk:
        raise CredentialError("official client requires TYREX_PRIVATE_KEY|POLYMARKET_PK")

    funder = loaded.funder or _funder_from_env(source) or None
    sig_t = loaded.signature_type
    if _signature_type_from_env(source) is not None:
        sig_t = _signature_type_from_env(source)  # type: ignore[assignment]

    client = ClobClient(
        CLOB_HOST,
        chain_id=int(_strip_env(source.get("TYREX_CHAIN_ID")) or "137"),
        key=pk if pk.startswith("0x") else "0x" + pk,
        signature_type=sig_t,
        funder=funder,
    )
    # Existing credentials only — never create/derive
    client.set_api_creds(
        ApiCreds(
            api_key=loaded.api_key,
            api_secret=loaded.secret,
            api_passphrase=loaded.passphrase,
        )
    )
    return client


@dataclass
class SdkReadonlyTransport:
    """Observation surface over official V2 client — no mutation methods."""

    _client: Any
    creds: L2Credentials
    spy: NetworkAllowlistSpy = field(default_factory=NetworkAllowlistSpy)
    _handlers: list[Callable] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> SdkReadonlyTransport:
        creds = load_l2_credentials(env)
        client = build_official_readonly_client(env=env, creds=creds)
        return cls(_client=client, creds=creds)

    def __getattribute__(self, name: str) -> Any:
        if name in _MUTATION_METHODS:
            raise MutationAttemptError(f"SdkReadonlyTransport forbids {name}")
        return super().__getattribute__(name)

    def get_open_orders(self, *, market_id: str | None = None) -> list[VenueOrderSnapshot]:
        self.spy.check(method="GET", path="/data/orders")
        kwargs: dict[str, Any] = {}
        if market_id:
            kwargs["market"] = market_id
        # Official SDK: get_open_orders(params=...) or bare
        try:
            if kwargs:
                from py_clob_client_v2.clob_types import OpenOrderParams

                raw = self._client.get_open_orders(OpenOrderParams(**kwargs))
            else:
                raw = self._client.get_open_orders()
        except TypeError:
            raw = self._client.get_open_orders()
        rows = raw if isinstance(raw, list) else (raw.get("data") if isinstance(raw, dict) else [])
        if not isinstance(rows, list):
            return []
        return [venue_order_from_rest(r if isinstance(r, dict) else dict(r)) for r in rows]

    def get_order(self, venue_order_id: str) -> VenueOrderSnapshot | None:
        self.spy.check(method="GET", path="/data/order")
        raw = self._client.get_order(venue_order_id)
        if raw is None:
            return None
        if isinstance(raw, dict):
            return venue_order_from_rest(raw)
        return venue_order_from_rest(dict(raw))

    def get_trades(
        self, *, market_id: str | None = None, after: str | None = None
    ) -> list[VenueTradeSnapshot]:
        self.spy.check(method="GET", path="/data/trades")
        try:
            raw = self._client.get_trades()
        except Exception:  # noqa: BLE001
            raw = []
        rows = raw if isinstance(raw, list) else []
        out = [venue_trade_from_rest(r if isinstance(r, dict) else dict(r)) for r in rows]
        if market_id:
            out = [t for t in out if t.market_id == market_id]
        return out

    def get_balance(self) -> VenueBalanceSnapshot:
        self.spy.check(method="GET", path="/balance-allowance")
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        raw = self._client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        if not isinstance(raw, dict):
            return VenueBalanceSnapshot(collateral_balance=Decimal("0"), allowance=None)
        bal = Decimal(str(raw.get("balance") or "0"))
        allowance = None
        allowances = raw.get("allowances")
        if isinstance(allowances, dict) and allowances:
            # Presence only downstream — keep decimal for internal use; preflight redacts
            first = next(iter(allowances.values()))
            allowance = Decimal(str(first))
        elif raw.get("allowance") is not None:
            allowance = Decimal(str(raw.get("allowance")))
        return VenueBalanceSnapshot(collateral_balance=bal, allowance=allowance)

    def get_conditional_balance_allowance(
        self, token_id: str
    ) -> tuple[Decimal, Decimal | None]:
        """CONDITIONAL balance/allowance for the funder/proxy owner (share units).

        When ``signature_type=1``, the official client is constructed with
        ``funder=proxy``; querying via this transport must never substitute the
        signer EOA as the conditional-token owner.
        """
        from tyrex_pm.execution.polymarket.address_roles import roles_from_credentials

        roles = roles_from_credentials(self.creds)
        roles.assert_conditional_query_target(queried_as=roles.conditional_owner)
        if roles.proxy_mode and roles.conditional_owner.lower() == roles.signer_eoa.lower():
            # Only legal when signer==funder; otherwise refuse
            if roles.funder_proxy and roles.funder_proxy.lower() != roles.signer_eoa.lower():
                from tyrex_pm.execution.polymarket.address_roles import AddressRoleError

                raise AddressRoleError("REFUSING_SIGNER_CONDITIONAL_BALANCE_WHEN_PROXY_MODE")

        self.spy.check(method="GET", path="/balance-allowance")
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        raw = self._client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        )
        if not isinstance(raw, dict):
            return Decimal("0"), None
        bal = Decimal(str(raw.get("balance") or "0")) / Decimal("1000000")
        allowance = None
        allowances = raw.get("allowances")
        if isinstance(allowances, dict) and allowances:
            first = next(iter(allowances.values()))
            allowance = Decimal(str(first)) / Decimal("1000000")
        elif raw.get("allowance") is not None:
            allowance = Decimal(str(raw.get("allowance"))) / Decimal("1000000")
        return bal, allowance

    def get_positions_raw(self) -> list[dict[str, Any]]:
        """Full Data API position rows for the funder/proxy (ack validation)."""
        import json
        from urllib.request import Request, urlopen

        user = positions_wallet_address(self.creds)
        url = f"https://data-api.polymarket.com/positions?{urlencode({'user': user})}"
        self.spy.check(method="GET", path="/positions")
        req = Request(url, method="GET", headers={"User-Agent": "tyrex-pm-r6d/1.0"})
        with urlopen(req, timeout=15) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict)]

    def get_positions(self) -> list[VenuePositionSnapshot]:
        # Public Data API — funder preferred (historical)
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
        self.spy.check(method="GET", path="/time")
        try:
            return int(self._client.get_server_time())
        except Exception:  # noqa: BLE001
            return None

    def subscribe_user_events(self, handler: Callable) -> None:
        self._handlers.append(handler)

    def stop(self) -> None:
        self._handlers.clear()
