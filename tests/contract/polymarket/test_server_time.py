"""Deterministic tests for the narrow official CLOB ``/time`` adapter."""

from __future__ import annotations

import io
import json
import socket
import ssl
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

from tyrex_pm.adapters.polymarket.clob_server_time import (
    CLOB_TIME_PATH,
    CLOB_TIME_URL,
    ClobServerTimeResponseError,
    ClobServerTimeTransportError,
    fetch_clob_server_time,
)


class _FakeResp:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _opener(body: Any, status: int = 200):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def open_fn(req: Any, timeout: float = 15.0) -> _FakeResp:  # noqa: ARG001
        assert req.full_url == CLOB_TIME_URL or str(req.full_url).endswith(CLOB_TIME_PATH)
        assert req.get_method() == "GET"
        headers = {str(k).lower(): v for k, v in dict(req.headers).items()}
        assert "authorization" not in headers
        assert "poly_api_key" not in headers
        assert "poly_signature" not in headers
        return _FakeResp(raw, status=status)

    return open_fn


def test_time_success_returns_unix_seconds_int() -> None:
    result = fetch_clob_server_time(opener=_opener(1_700_000_000))
    assert result.unix_seconds == 1_700_000_000
    assert isinstance(result.unix_seconds, int)
    assert type(result.unix_seconds) is int
    assert result.http_status == 200


def test_endpoint_path_is_exactly_time() -> None:
    assert CLOB_TIME_PATH == "/time"
    assert CLOB_TIME_URL.endswith("/time")

    seen: dict[str, str] = {}

    def open_fn(req: Any, timeout: float = 15.0) -> _FakeResp:  # noqa: ARG001
        seen["path"] = req.selector if hasattr(req, "selector") else req.full_url
        seen["url"] = req.full_url
        return _FakeResp(b"1700000000")

    fetch_clob_server_time(opener=open_fn)
    assert seen["url"] == CLOB_TIME_URL
    assert "/time" in seen["url"]
    assert seen["url"].rstrip("/").endswith("/time")


@pytest.mark.parametrize(
    "body",
    [
        True,
        False,
        "1700000000",
        1700000000.5,
        1700000000.0,
        None,
        {"t": 1},
        [1_700_000_000],
    ],
)
def test_rejects_non_integer_json(body: Any) -> None:
    with pytest.raises(ClobServerTimeResponseError) as ei:
        fetch_clob_server_time(opener=_opener(body))
    assert ei.value.venue_reached is True
    assert ei.value.failure_kind == "invalid_time_response"


def test_rejects_malformed_json() -> None:
    with pytest.raises(ClobServerTimeResponseError) as ei:
        fetch_clob_server_time(opener=_opener(b"not-json"))
    assert ei.value.failure_kind == "invalid_time_response"
    assert ei.value.venue_reached is True


def test_http_4xx_is_response_not_transport() -> None:
    def open_fn(req: Any, timeout: float = 15.0) -> _FakeResp:  # noqa: ARG001
        raise HTTPError(CLOB_TIME_URL, 404, "no", hdrs=None, fp=io.BytesIO(b'{"error":"x"}'))

    with pytest.raises(ClobServerTimeResponseError) as ei:
        fetch_clob_server_time(opener=open_fn)
    assert ei.value.venue_reached is True
    assert ei.value.failure_kind == "http_client_error"
    assert ei.value.http_status == 404


def test_http_5xx_is_response_not_transport() -> None:
    def open_fn(req: Any, timeout: float = 15.0) -> _FakeResp:  # noqa: ARG001
        raise HTTPError(CLOB_TIME_URL, 503, "unavailable", hdrs=None, fp=io.BytesIO(b""))

    with pytest.raises(ClobServerTimeResponseError) as ei:
        fetch_clob_server_time(opener=open_fn)
    assert ei.value.venue_reached is True
    assert ei.value.failure_kind == "http_server_error"


def test_timeout_is_transport() -> None:
    def open_fn(req: Any, timeout: float = 15.0) -> _FakeResp:  # noqa: ARG001
        raise TimeoutError("timed out")

    with pytest.raises(ClobServerTimeTransportError):
        fetch_clob_server_time(opener=open_fn)


def test_dns_is_transport() -> None:
    def open_fn(req: Any, timeout: float = 15.0) -> _FakeResp:  # noqa: ARG001
        raise URLError(socket.gaierror(11001, "getaddrinfo failed"))

    with pytest.raises(ClobServerTimeTransportError) as ei:
        fetch_clob_server_time(opener=open_fn)
    assert "dns" in str(ei.value)


def test_tls_is_transport() -> None:
    def open_fn(req: Any, timeout: float = 15.0) -> _FakeResp:  # noqa: ARG001
        raise URLError(ssl.SSLError("CERTIFICATE_VERIFY_FAILED"))

    with pytest.raises(ClobServerTimeTransportError) as ei:
        fetch_clob_server_time(opener=open_fn)
    assert "tls" in str(ei.value)


def test_connection_error_is_transport() -> None:
    def open_fn(req: Any, timeout: float = 15.0) -> _FakeResp:  # noqa: ARG001
        raise URLError(ConnectionRefusedError("refused"))

    with pytest.raises(ClobServerTimeTransportError) as ei:
        fetch_clob_server_time(opener=open_fn)
    assert "connection" in str(ei.value)


def test_no_fallback_on_adapter_failure() -> None:
    def open_fn(req: Any, timeout: float = 15.0) -> _FakeResp:  # noqa: ARG001
        raise URLError(TimeoutError("timed out"))

    with pytest.raises(ClobServerTimeTransportError):
        fetch_clob_server_time(opener=open_fn)
