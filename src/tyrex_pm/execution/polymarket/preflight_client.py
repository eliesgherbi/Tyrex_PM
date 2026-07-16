"""Concrete read-only client with NO submit/cancel methods (R6C)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tyrex_pm.execution.polymarket.auth import L2Credentials, redact_text
from tyrex_pm.execution.polymarket.normalize import venue_order_from_rest, venue_trade_from_rest
from tyrex_pm.execution.polymarket.readonly_transport import (
    AUTH_CLOB_BALANCE,
    AUTH_CLOB_ORDERS,
    AUTH_CLOB_TRADES,
    EndpointProbeResult,
    PUBLIC_CLOB_BOOK,
    PUBLIC_CLOB_TIME,
    PUBLIC_DATA_POSITIONS,
    UserEventHandler,
)
from tyrex_pm.execution.polymarket.transport import (
    VenueBalanceSnapshot,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
    VenueTradeSnapshot,
)

CLOB_BASE = "https://clob.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"


@dataclass
class PreflightReadClient:
    """Account observation client. Structurally lacks submit/cancel/heartbeat."""

    creds: L2Credentials | None = None
    base_url: str = CLOB_BASE
    timeout_s: float = 15.0
    probes: list[EndpointProbeResult] = field(default_factory=list)
    _handlers: list[UserEventHandler] = field(default_factory=list)

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

    def _classify_http_error(self, code: int, body: str) -> tuple[str | None, bool]:
        """Return (cloudflare_error, app_auth_reached)."""
        low = body.lower()
        if "1010" in body or "error code: 1010" in low:
            return "1010", False
        if "cloudflare" in low and code in {403, 503}:
            return "cloudflare", False
        # Application auth typically returns JSON error bodies, not CF HTML/codes
        if code in {401, 400} and ("error" in low or body.strip().startswith("{")):
            return None, True
        if code == 403 and body.strip().startswith("{"):
            return None, True
        if code == 403:
            return "403_non_json", False
        return None, code not in {403, 503, 520, 521, 522, 523, 524}

    def get_server_time(self) -> int | None:
        host, path, method, cat = PUBLIC_CLOB_TIME
        url = f"https://{host}{path}"
        req = Request(url, method=method, headers={"User-Agent": "tyrex-pm-r6c/1.0"})
        try:
            with urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8")
                self._record(
                    host=host,
                    path=path,
                    method=method,
                    category=cat,
                    status=int(resp.status),
                    cloudflare_error=None,
                    app_auth_validation_reached=False,
                    ok=True,
                )
                return int(json.loads(raw))
        except HTTPError as exc:
            body = exc.read(300).decode("utf-8", errors="replace")
            cf, auth = self._classify_http_error(exc.code, body)
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status=int(exc.code),
                cloudflare_error=cf,
                app_auth_validation_reached=auth,
                ok=False,
            )
            return None
        except URLError:
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status="network_error",
                cloudflare_error=None,
                app_auth_validation_reached=False,
                ok=False,
            )
            return None

    def probe_public_book(self, token_id: str = "1") -> bool:
        host, path, method, cat = PUBLIC_CLOB_BOOK
        url = f"https://{host}{path}?{urlencode({'token_id': token_id})}"
        req = Request(url, method=method, headers={"User-Agent": "tyrex-pm-r6c/1.0"})
        try:
            with urlopen(req, timeout=self.timeout_s) as resp:
                self._record(
                    host=host,
                    path=path,
                    method=method,
                    category=cat,
                    status=int(resp.status),
                    cloudflare_error=None,
                    app_auth_validation_reached=False,
                    ok=True,
                )
                return True
        except HTTPError as exc:
            body = exc.read(300).decode("utf-8", errors="replace")
            cf, auth = self._classify_http_error(exc.code, body)
            # 400/404 on synthetic token still proves public app reached (not CF)
            ok = exc.code in {400, 404} and cf is None
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status=int(exc.code),
                cloudflare_error=cf,
                app_auth_validation_reached=False,
                ok=ok,
                detail="expected_bad_token" if ok else None,
            )
            return ok
        except URLError:
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status="network_error",
                cloudflare_error=None,
                app_auth_validation_reached=False,
                ok=False,
            )
            return False

    def _l2_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        if self.creds is None:
            raise RuntimeError("credentials_missing")
        import base64
        import hashlib
        import hmac

        ts = str(int(time.time()))
        message = ts + method.upper() + path + body
        secret = base64.urlsafe_b64decode(self.creds.secret)
        sig = base64.urlsafe_b64encode(
            hmac.new(secret, message.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        return {
            "POLY_ADDRESS": self.creds.address,
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": ts,
            "POLY_API_KEY": self.creds.api_key,
            "POLY_PASSPHRASE": self.creds.passphrase,
            "Content-Type": "application/json",
            "User-Agent": "tyrex-pm-r6c/1.0",
        }

    def _auth_get(self, path: str, *, params: dict[str, str] | None, category: str) -> Any:
        host = "clob.polymarket.com"
        qs = f"?{urlencode(params)}" if params else ""
        headers = self._l2_headers("GET", path)
        req = Request(self.base_url + path + qs, headers=headers, method="GET")
        try:
            with urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self._record(
                    host=host,
                    path=path,
                    method="GET",
                    category=category,
                    status=int(resp.status),
                    cloudflare_error=None,
                    app_auth_validation_reached=True,
                    ok=True,
                )
                return data
        except HTTPError as exc:
            body = exc.read(300).decode("utf-8", errors="replace")
            if self.creds is not None:
                body = redact_text(body, self.creds)
            cf, auth = self._classify_http_error(exc.code, body)
            self._record(
                host=host,
                path=path,
                method="GET",
                category=category,
                status=int(exc.code),
                cloudflare_error=cf,
                app_auth_validation_reached=auth,
                ok=False,
            )
            raise RuntimeError(f"HTTP {exc.code} on {path}") from None
        except URLError as exc:
            self._record(
                host=host,
                path=path,
                method="GET",
                category=category,
                status="network_error",
                cloudflare_error=None,
                app_auth_validation_reached=False,
                ok=False,
            )
            raise RuntimeError(f"network error on {path}: {type(exc.reason).__name__}") from None

    def get_open_orders(self, *, market_id: str | None = None) -> list[VenueOrderSnapshot]:
        params = {} if market_id is None else {"market": market_id}
        raw = self._auth_get(AUTH_CLOB_ORDERS[1], params=params or None, category=AUTH_CLOB_ORDERS[3])
        rows = raw.get("data") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return []
        return [venue_order_from_rest(r) for r in rows if isinstance(r, dict)]

    def get_order(self, venue_order_id: str) -> VenueOrderSnapshot | None:
        raw = self._auth_get(AUTH_CLOB_ORDERS[1], params={"id": venue_order_id}, category=AUTH_CLOB_ORDERS[3])
        rows = raw.get("data") if isinstance(raw, dict) else raw
        if not isinstance(rows, list) or not rows:
            return None
        return venue_order_from_rest(rows[0])

    def get_trades(
        self, *, market_id: str | None = None, after: str | None = None
    ) -> list[VenueTradeSnapshot]:
        params: dict[str, str] = {}
        if market_id:
            params["market"] = market_id
        if after:
            params["after"] = after
        raw = self._auth_get(AUTH_CLOB_TRADES[1], params=params or None, category=AUTH_CLOB_TRADES[3])
        rows = raw.get("data") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return []
        return [venue_trade_from_rest(r) for r in rows if isinstance(r, dict)]

    def get_balance(self) -> VenueBalanceSnapshot:
        raw = self._auth_get(
            AUTH_CLOB_BALANCE[1],
            params={"asset_type": "COLLATERAL"},
            category=AUTH_CLOB_BALANCE[3],
        )
        if not isinstance(raw, dict):
            return VenueBalanceSnapshot(collateral_balance=Decimal("0"), allowance=None)
        return VenueBalanceSnapshot(
            collateral_balance=Decimal(str(raw.get("balance") or "0")),
            allowance=(
                None if raw.get("allowance") is None else Decimal(str(raw.get("allowance")))
            ),
        )

    def get_positions(self) -> list[VenuePositionSnapshot]:
        """Public Data API positions by user address (no CLOB L2)."""
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
        # Address used in query — report only presence, not value, in facts elsewhere.
        url = f"{DATA_API_BASE}{path}?{urlencode({'user': self.creds.address})}"
        req = Request(url, method=method, headers={"User-Agent": "tyrex-pm-r6c/1.0"})
        try:
            with urlopen(req, timeout=self.timeout_s) as resp:
                rows = json.loads(resp.read().decode("utf-8"))
                self._record(
                    host=host,
                    path=path,
                    method=method,
                    category=cat,
                    status=int(resp.status),
                    cloudflare_error=None,
                    app_auth_validation_reached=False,
                    ok=True,
                )
        except HTTPError as exc:
            body = exc.read(300).decode("utf-8", errors="replace")
            cf, _ = self._classify_http_error(exc.code, body)
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status=int(exc.code),
                cloudflare_error=cf,
                app_auth_validation_reached=False,
                ok=False,
            )
            return []
        except URLError:
            self._record(
                host=host,
                path=path,
                method=method,
                category=cat,
                status="network_error",
                cloudflare_error=None,
                app_auth_validation_reached=False,
                ok=False,
            )
            return []
        out: list[VenuePositionSnapshot] = []
        if not isinstance(rows, list):
            return out
        for r in rows:
            if not isinstance(r, dict):
                continue
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

    def subscribe_user_events(self, handler: UserEventHandler) -> None:
        # Connection managed by preflight runner; store handler only.
        self._handlers.append(handler)

    def stop(self) -> None:
        self._handlers.clear()
