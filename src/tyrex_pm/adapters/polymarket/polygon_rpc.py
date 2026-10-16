"""Read-only Polygon JSON-RPC for startup payout proofs.

The endpoint comes from ``TYREX_POLYGON_RPC_URL``. This module never logs that
URL, query tokens, or request headers. It sends only ``eth_chainId`` and
``eth_call`` with the ``latest`` tag. A provider that is not Polygon mainnet
(chain id 137) is rejected before any payout result is used.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Awaitable, Callable, Mapping

from tyrex_pm.adapters.polymarket.ctf_payout import POLYGON_CHAIN_ID

POLYGON_RPC_ENV = "TYREX_POLYGON_RPC_URL"
_PLACEHOLDER_URL = "https://your-polygon-rpc-endpoint"
_REQUEST_ID = 1
_TRANSIENT_HTTP = frozenset({408, 429, 500, 502, 503, 504})
_ALLOWED_METHODS = frozenset({"eth_chainId", "eth_call"})

Poster = Callable[[str, list[Any]], Awaitable[tuple[int, Any]]]


class PolygonRpcError(RuntimeError):
    """Fail-closed RPC error. The message never includes the endpoint."""


def payout_caller_from_env(env: Mapping[str, str] | None) -> Callable[..., Awaitable[str]] | None:
    """Return an ``eth_call`` seam, or None when no RPC URL is configured."""
    if not env:
        return None
    url = str(env.get(POLYGON_RPC_ENV) or "").strip()
    if not url:
        return None
    if url.rstrip("/").lower() == _PLACEHOLDER_URL:
        raise PolygonRpcError("polygon_rpc_placeholder")
    lowered = url.lower()
    if not lowered.startswith(("https://", "http://")):
        raise PolygonRpcError("polygon_rpc_url_scheme")
    return PolygonRpcReader(url).eth_call


class PolygonRpcReader:
    """Small bounded reader. One chain check, then ``eth_call`` at ``latest``."""

    # Subclasses with a wider read-only surface override this set explicitly.
    allowed_methods: frozenset[str] = _ALLOWED_METHODS

    def __init__(
        self,
        url: str,
        *,
        timeout_s: float = 5.0,
        attempts: int = 3,
        retry_delay_s: float = 0.25,
        poster: Poster | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise PolygonRpcError("polygon_rpc_timeout_invalid")
        if attempts < 1:
            raise PolygonRpcError("polygon_rpc_attempts_invalid")
        self._url = url
        self.timeout_s = timeout_s
        self.attempts = attempts
        self.retry_delay_s = retry_delay_s
        self._poster = poster
        self._chain_ok = False

    async def eth_call(self, *, to: str, data: str) -> str:
        await self._ensure_chain()
        result = await self._rpc(
            "eth_call",
            [{"to": to, "data": data}, "latest"],
        )
        if not _abi_word(result):
            raise PolygonRpcError("polygon_rpc_malformed")
        return result

    async def _ensure_chain(self) -> None:
        if self._chain_ok:
            return
        result = await self._rpc("eth_chainId", [])
        chain_id = _chain_id(result)
        if chain_id is None:
            raise PolygonRpcError("polygon_rpc_malformed")
        if chain_id != POLYGON_CHAIN_ID:
            raise PolygonRpcError("polygon_rpc_wrong_chain")
        self._chain_ok = True

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        if method not in self.allowed_methods:
            raise PolygonRpcError("polygon_rpc_method")
        last = "polygon_rpc_exhausted"
        for attempt in range(self.attempts):
            try:
                status, body = await self._post(method, params)
            except (TimeoutError, OSError):
                last = "polygon_rpc_timeout"
                if attempt + 1 >= self.attempts:
                    break
                await asyncio.sleep(self.retry_delay_s * (attempt + 1))
                continue
            if status in _TRANSIENT_HTTP:
                last = "polygon_rpc_retry"
                if attempt + 1 >= self.attempts:
                    break
                await asyncio.sleep(self.retry_delay_s * (attempt + 1))
                continue
            if status != 200 or not isinstance(body, dict):
                raise PolygonRpcError("polygon_rpc_malformed")
            outcome, payload = _decode_envelope(body)
            if outcome == "error":
                if _transient_rpc_error(payload) and attempt + 1 < self.attempts:
                    await asyncio.sleep(self.retry_delay_s * (attempt + 1))
                    continue
                raise PolygonRpcError("polygon_rpc_error")
            return payload
        raise PolygonRpcError(last)

    async def _post(self, method: str, params: list[Any]) -> tuple[int, Any]:
        if self._poster is not None:
            return await self._poster(method, params)
        return await asyncio.to_thread(self._post_urllib, method, params)

    def _post_urllib(self, method: str, params: list[Any]) -> tuple[int, Any]:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": _REQUEST_ID, "method": method, "params": params}
        ).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                status = int(getattr(response, "status", 200))
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                raw = exc.read()
            except OSError:
                raw = b""
        except TimeoutError:
            raise TimeoutError("polygon_rpc_timeout") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError("polygon_rpc_timeout") from None
            raise OSError("polygon_rpc_transport") from None
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        return status, parsed


def _decode_envelope(body: Mapping[str, Any]) -> tuple[str, Any]:
    """Accept one JSON-RPC 2.0 outcome whose id matches this request."""
    if body.get("jsonrpc") != "2.0" or body.get("id") != _REQUEST_ID:
        raise PolygonRpcError("polygon_rpc_malformed")
    has_result = "result" in body
    has_error = "error" in body
    if has_result == has_error:
        raise PolygonRpcError("polygon_rpc_malformed")
    if has_error:
        return "error", body.get("error")
    return "result", body.get("result")


def _abi_word(result: Any) -> bool:
    """A uint256 eth_call result is 0x plus exactly 32 bytes."""
    if not isinstance(result, str) or not result.startswith("0x"):
        return False
    body = result[2:]
    if len(body) != 64:
        return False
    return all(ch in "0123456789abcdef" for ch in body.lower())


def _chain_id(result: Any) -> int | None:
    if not isinstance(result, str) or not result.lower().startswith("0x"):
        return None
    try:
        return int(result, 16)
    except ValueError:
        return None


def _transient_rpc_error(error: Any) -> bool:
    if not isinstance(error, dict):
        return False
    code = error.get("code")
    if code in _TRANSIENT_HTTP or code in {-32005, -32016}:
        return True
    message = str(error.get("message") or "").lower()
    return "rate limit" in message or "timeout" in message or "temporarily" in message
