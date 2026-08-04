"""Concrete read-only client with NO submit/cancel methods (R6C).

Authenticated CLOB I/O goes through official ``polymarket-client``.
Public server time uses the narrow official ``/time`` adapter only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tyrex_pm.adapters.polymarket.clob_server_time import (
    ClobServerTimeResponseError,
    ClobServerTimeTransportError,
    fetch_clob_server_time,
)
from tyrex_pm.adapters.polymarket.sdk_errors import classify_polymarket_error
from tyrex_pm.execution.polymarket.auth import L2Credentials, positions_wallet_address, redact_text
from tyrex_pm.execution.polymarket.readonly_transport import (
    AUTH_CLOB_BALANCE,
    AUTH_CLOB_ORDERS,
    AUTH_CLOB_TRADES,
    PUBLIC_CLOB_TIME,
    PUBLIC_DATA_POSITIONS,
    EndpointProbeResult,
    UserEventHandler,
)
from tyrex_pm.execution.polymarket.sdk_readonly import SdkReadonlyTransport
from tyrex_pm.execution.polymarket.transport import (
    VenueBalanceSnapshot,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
    VenueTradeSnapshot,
)

CLOB_BASE = "https://clob.polymarket.com"


@dataclass
class PreflightReadClient:
    """Account observation client. Structurally lacks submit/cancel/heartbeat."""

    creds: L2Credentials | None = None
    base_url: str = CLOB_BASE
    timeout_s: float = 15.0
    probes: list[EndpointProbeResult] = field(default_factory=list)
    _handlers: list[UserEventHandler] = field(default_factory=list)
    _ro: SdkReadonlyTransport | None = field(default=None, repr=False)
    last_public_clob: dict[str, Any] = field(default_factory=dict)

    def _record(
        self,
        *,
        host: str,
        path: str,
        method: str,
        category: str,
        status: int | str,
        cloudflare_error: str | None,
        app_auth_validation_reached: bool,
        ok: bool,
        detail: str | None = None,
    ) -> EndpointProbeResult:
        row = EndpointProbeResult(
            host=host,
            path=path,
            method=method,
            category=category,
            status=status,
            cloudflare_error=cloudflare_error,
            app_auth_validation_reached=app_auth_validation_reached,
            ok=ok,
            detail=detail,
        )
        self.probes.append(row)
        return row

    def _classify_exc(self, exc: BaseException) -> tuple[str | None, bool]:
        classified = classify_polymarket_error(exc)
        msg = classified.message.lower()
        if "1010" in msg:
            return "1010", False
        if "cloudflare" in msg:
            return "cloudflare", False
        if classified.category.value == "AUTH":
            return None, True
        return None, classified.category.value in {"REJECTED", "USER_INPUT"}

    def _readonly(self) -> SdkReadonlyTransport:
        if self._ro is None:
            if self.creds is None:
                raise RuntimeError("credentials_missing")
            self._ro = SdkReadonlyTransport.from_env()
            self._ro.creds = self.creds
        return self._ro

    def get_server_time(self) -> int | None:
        """Fetch official CLOB Unix seconds via the narrow ``/time`` adapter.

        Records separately observable connectivity vs venue-time validity in
        ``last_public_clob``. Never probes order books or synthetic tokens.
        """
        host, path, method, cat = PUBLIC_CLOB_TIME
        try:
            result = fetch_clob_server_time(timeout_s=self.timeout_s)
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status=int(result.http_status),
                cloudflare_error=None,
                app_auth_validation_reached=False,
                ok=True,
                detail="clob_server_time_adapter",
            )
            self.last_public_clob = {
                "transport_reachable": True,
                "venue_time_valid": True,
                "venue_time_unix_s": int(result.unix_seconds),
                "failure_kind": None,
                "http_status": int(result.http_status),
                "detail": None,
            }
            return int(result.unix_seconds)
        except ClobServerTimeTransportError as exc:
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status=type(exc).__name__,
                cloudflare_error=None,
                app_auth_validation_reached=False,
                ok=False,
                detail=str(exc)[:120],
            )
            self.last_public_clob = {
                "transport_reachable": False,
                "venue_time_valid": False,
                "venue_time_unix_s": None,
                "failure_kind": "transport",
                "http_status": None,
                "detail": str(exc)[:120],
            }
            return None
        except ClobServerTimeResponseError as exc:
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status=exc.http_status if exc.http_status is not None else type(exc).__name__,
                cloudflare_error=None,
                app_auth_validation_reached=False,
                ok=False,
                detail=str(exc)[:120],
            )
            self.last_public_clob = {
                "transport_reachable": bool(exc.venue_reached),
                "venue_time_valid": False,
                "venue_time_unix_s": None,
                "failure_kind": exc.failure_kind,
                "http_status": exc.http_status,
                "detail": str(exc)[:120],
            }
            return None

    def get_open_orders(self, *, market_id: str | None = None) -> list[VenueOrderSnapshot]:
        host, path, method, cat = AUTH_CLOB_ORDERS
        try:
            rows = self._readonly().get_open_orders(market_id=market_id)
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status=200,
                cloudflare_error=None,
                app_auth_validation_reached=True,
                ok=True,
                detail="polymarket-client",
            )
            return rows
        except Exception as exc:  # noqa: BLE001
            cf, auth = self._classify_exc(exc)
            detail = str(exc)
            if self.creds is not None:
                detail = redact_text(detail, self.creds)
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status=type(exc).__name__,
                cloudflare_error=cf,
                app_auth_validation_reached=auth,
                ok=False,
                detail=detail[:120],
            )
            raise RuntimeError(f"SDK error on {path}") from exc

    def get_order(self, venue_order_id: str) -> VenueOrderSnapshot | None:
        return self._readonly().get_order(venue_order_id)

    def get_trades(
        self, *, market_id: str | None = None, after: str | None = None
    ) -> list[VenueTradeSnapshot]:
        host, path, method, cat = AUTH_CLOB_TRADES
        try:
            rows = self._readonly().get_trades(market_id=market_id, after=after)
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status=200,
                cloudflare_error=None,
                app_auth_validation_reached=True,
                ok=True,
            )
            return rows
        except Exception as exc:  # noqa: BLE001
            cf, auth = self._classify_exc(exc)
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status=type(exc).__name__,
                cloudflare_error=cf,
                app_auth_validation_reached=auth,
                ok=False,
            )
            raise RuntimeError(f"SDK error on {path}") from exc

    def get_balance(self) -> VenueBalanceSnapshot:
        host, path, method, cat = AUTH_CLOB_BALANCE
        try:
            bal = self._readonly().get_balance()
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status=200,
                cloudflare_error=None,
                app_auth_validation_reached=True,
                ok=True,
            )
            return bal
        except Exception as exc:  # noqa: BLE001
            cf, auth = self._classify_exc(exc)
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status=type(exc).__name__,
                cloudflare_error=cf,
                app_auth_validation_reached=auth,
                ok=False,
            )
            raise RuntimeError(f"SDK error on {path}") from exc

    def get_positions(self) -> list[VenuePositionSnapshot]:
        host, path, method, cat = PUBLIC_DATA_POSITIONS
        if self.creds is None:
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status="skipped",
                cloudflare_error=None,
                app_auth_validation_reached=False,
                ok=False,
                detail="credentials_missing",
            )
            return []
        _ = positions_wallet_address(self.creds)
        try:
            rows = self._readonly().get_positions()
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status=200,
                cloudflare_error=None,
                app_auth_validation_reached=False,
                ok=True,
                detail="polymarket-client",
            )
            return rows
        except Exception as exc:  # noqa: BLE001
            cf, _ = self._classify_exc(exc)
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status=type(exc).__name__,
                cloudflare_error=cf,
                app_auth_validation_reached=False,
                ok=False,
            )
            return []

    def subscribe_user_events(self, handler: UserEventHandler) -> None:
        self._handlers.append(handler)

    def stop(self) -> None:
        self._handlers.clear()
        if self._ro is not None:
            self._ro.stop()
            self._ro = None
