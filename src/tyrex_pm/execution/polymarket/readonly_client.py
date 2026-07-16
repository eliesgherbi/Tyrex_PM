"""R6B authenticated read-only client — never submits or cancels."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tyrex_pm.execution.polymarket.auth import L2Credentials, redact_text
from tyrex_pm.execution.polymarket.normalize import venue_order_from_rest, venue_trade_from_rest
from tyrex_pm.execution.polymarket.transport import (
    CancelOrderResult,
    SubmitOrderRequest,
    SubmitOrderResult,
    VenueBalanceSnapshot,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
    VenueTradeSnapshot,
)

CLOB_BASE = "https://clob.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"


class MutationAttemptError(RuntimeError):
    """Raised if R6B client is asked to mutate."""


@dataclass
class ReadOnlyClobClient:
    """L2-authenticated reads only. ``submit_order`` / ``cancel_order`` always fail."""

    creds: L2Credentials
    base_url: str = CLOB_BASE
    timeout_s: float = 15.0

    def submit_order(self, request: SubmitOrderRequest) -> SubmitOrderResult:
        raise MutationAttemptError("R6B read-only client forbids submit_order")

    def cancel_order(self, venue_order_id: str) -> CancelOrderResult:
        raise MutationAttemptError("R6B read-only client forbids cancel_order")

    def _l2_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        # Minimal HMAC compatible with Polymarket L2 (see official signing docs).
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
        }

    def _get_json(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        qs = f"?{urlencode(params)}" if params else ""
        url_path = path + qs
        headers = self._l2_headers("GET", path)
        req = Request(self.base_url + url_path, headers=headers, method="GET")
        try:
            with urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            safe = redact_text(body, self.creds)
            raise RuntimeError(f"HTTP {exc.code} on {path}: {safe[:200]}") from None
        except URLError as exc:
            raise RuntimeError(f"network error on {path}: {type(exc.reason).__name__}") from None

    def get_open_orders(self, *, market_id: str | None = None) -> list[VenueOrderSnapshot]:
        params = {} if market_id is None else {"market": market_id}
        raw = self._get_json("/data/orders", params=params or None)
        rows = raw.get("data") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return []
        return [venue_order_from_rest(r) for r in rows if isinstance(r, dict)]

    def get_order(self, venue_order_id: str) -> VenueOrderSnapshot | None:
        raw = self._get_json("/data/orders", params={"id": venue_order_id})
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
        raw = self._get_json("/data/trades", params=params or None)
        rows = raw.get("data") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return []
        return [venue_trade_from_rest(r) for r in rows if isinstance(r, dict)]

    def get_positions(self) -> list[VenuePositionSnapshot]:
        # Public data-api by address — no L2 mutation surface.
        url = f"{DATA_API_BASE}/positions?{urlencode({'user': self.creds.address})}"
        req = Request(url, method="GET")
        with urlopen(req, timeout=self.timeout_s) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
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
                        None
                        if r.get("avgPrice") is None
                        else Decimal(str(r.get("avgPrice")))
                    ),
                    market_id=None if r.get("conditionId") is None else str(r.get("conditionId")),
                )
            )
        return out

    def get_balance(self) -> VenueBalanceSnapshot:
        raw = self._get_json(
            "/balance-allowance",
            params={"asset_type": "COLLATERAL"},
        )
        if not isinstance(raw, dict):
            return VenueBalanceSnapshot(collateral_balance=Decimal("0"), allowance=None)
        return VenueBalanceSnapshot(
            collateral_balance=Decimal(str(raw.get("balance") or "0")),
            allowance=(
                None if raw.get("allowance") is None else Decimal(str(raw.get("allowance")))
            ),
        )

    def subscribe_user_events(self, handler) -> None:  # noqa: ANN001
        raise MutationAttemptError("R6B does not open user-stream in automated mode")

    def stop(self) -> None:
        return
