"""Narrow official Polymarket CLOB server-time adapter.

``polymarket-client==0.2.0`` does not expose ``GET /time``. This module is the
single authoritative Tyrex implementation for that documented public endpoint
only — not a general raw CLOB REST client.

Official contract (docs.polymarket.com):
  GET https://clob.polymarket.com/time
  → application/json integer (Unix timestamp, seconds)
  → no authentication
"""

from __future__ import annotations

import json
import socket
import ssl
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CLOB_TIME_HOST = "clob.polymarket.com"
CLOB_TIME_PATH = "/time"
CLOB_TIME_URL = f"https://{CLOB_TIME_HOST}{CLOB_TIME_PATH}"
DEFAULT_TIMEOUT_S = 15.0
_USER_AGENT = "tyrex-pm-clob-server-time/1.0"


class ClobServerTimeError(Exception):
    """Base error for the narrow ``/time`` adapter."""


class ClobServerTimeTransportError(ClobServerTimeError):
    """DNS, TLS, connection, or timeout failure — venue not demonstrably reached."""


class ClobServerTimeResponseError(ClobServerTimeError):
    """HTTP or schema failure after the venue (or an edge) responded."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        venue_reached: bool = True,
        failure_kind: str = "invalid_time_response",
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.venue_reached = venue_reached
        self.failure_kind = failure_kind


@dataclass(frozen=True, kw_only=True)
class ClobServerTimeResult:
    unix_seconds: int
    http_status: int


class _HttpResponse(Protocol):
    status: int

    def read(self) -> bytes: ...

    def __enter__(self) -> _HttpResponse: ...

    def __exit__(self, *args: object) -> None: ...


UrlOpen = Callable[..., _HttpResponse]


def _validate_unix_seconds(parsed: Any) -> int:
    """Accept only JSON integers (reject bool/float/str/null/object/list)."""
    if type(parsed) is not int:
        raise ClobServerTimeResponseError(
            f"invalid_time_response_type:{type(parsed).__name__}",
            failure_kind="invalid_time_response",
        )
    # Reject absurd non-unix magnitudes without inventing offset sync.
    if parsed < 0:
        raise ClobServerTimeResponseError(
            "invalid_time_response_negative",
            failure_kind="invalid_time_response",
        )
    return parsed


def fetch_clob_server_time(
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    url: str = CLOB_TIME_URL,
    opener: UrlOpen | None = None,
) -> ClobServerTimeResult:
    """GET official ``/time`` and return Unix seconds.

    No authentication headers are attached. Callers must not add them.
    """
    open_fn: UrlOpen = opener or urlopen
    req = Request(
        url,
        method="GET",
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
    )
    # Refuse accidental auth leakage via Request headers from callers — we own Request.
    if any(h.lower() in {"authorization", "poly_api_key", "poly_signature"} for h in req.headers):
        raise RuntimeError("clob_server_time_must_not_send_auth")

    try:
        with open_fn(req, timeout=timeout_s) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            raw = resp.read()
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read(200).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = ""
        code = int(exc.code)
        if 400 <= code < 500:
            kind = "http_client_error"
        elif code >= 500:
            kind = "http_server_error"
        else:
            kind = "invalid_time_response"
        raise ClobServerTimeResponseError(
            f"http_{code}:{body[:120]}",
            http_status=code,
            venue_reached=True,
            failure_kind=kind,
        ) from exc
    except TimeoutError as exc:
        raise ClobServerTimeTransportError(f"timeout:{exc}") from exc
    except ssl.SSLError as exc:
        raise ClobServerTimeTransportError(f"tls:{exc}") from exc
    except socket.gaierror as exc:
        raise ClobServerTimeTransportError(f"dns:{exc}") from exc
    except socket.timeout as exc:
        raise ClobServerTimeTransportError(f"timeout:{exc}") from exc
    except URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLError):
            raise ClobServerTimeTransportError(f"tls:{reason}") from exc
        if isinstance(reason, socket.gaierror):
            raise ClobServerTimeTransportError(f"dns:{reason}") from exc
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise ClobServerTimeTransportError(f"timeout:{reason}") from exc
        raise ClobServerTimeTransportError(f"connection:{reason}") from exc
    except OSError as exc:
        raise ClobServerTimeTransportError(f"connection:{exc}") from exc

    if status >= 500:
        raise ClobServerTimeResponseError(
            f"http_{status}",
            http_status=status,
            venue_reached=True,
            failure_kind="http_server_error",
        )
    if status >= 400:
        raise ClobServerTimeResponseError(
            f"http_{status}",
            http_status=status,
            venue_reached=True,
            failure_kind="http_client_error",
        )
    if status != 200:
        raise ClobServerTimeResponseError(
            f"http_{status}",
            http_status=status,
            venue_reached=True,
            failure_kind="invalid_time_response",
        )

    try:
        text = raw.decode("utf-8")
        parsed = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClobServerTimeResponseError(
            "invalid_time_response_malformed_json",
            http_status=status,
            venue_reached=True,
            failure_kind="invalid_time_response",
        ) from exc

    unix_seconds = _validate_unix_seconds(parsed)
    return ClobServerTimeResult(unix_seconds=unix_seconds, http_status=status)
