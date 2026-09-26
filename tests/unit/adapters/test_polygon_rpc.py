"""Contract tests for the read-only Polygon payout RPC adapter."""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tyrex_pm.adapters.polymarket.ctf_payout import (
    CONDITIONAL_TOKENS_ADDRESS,
    PAYOUT_DENOMINATOR_SELECTOR,
    read_binary_payout,
)
from tyrex_pm.adapters.polymarket.polygon_rpc import (
    POLYGON_RPC_ENV,
    PolygonRpcError,
    PolygonRpcReader,
    payout_caller_from_env,
)

CONDITION = "0x" + "11" * 32


def _ok(result: str) -> tuple[int, dict]:
    return 200, {"jsonrpc": "2.0", "id": 1, "result": result}


def test_missing_rpc_url_does_not_invent_a_caller() -> None:
    assert payout_caller_from_env({}) is None
    assert payout_caller_from_env({POLYGON_RPC_ENV: "  "}) is None


def test_placeholder_endpoint_is_rejected_before_a_connection() -> None:
    with pytest.raises(PolygonRpcError, match="polygon_rpc_placeholder") as caught:
        payout_caller_from_env({POLYGON_RPC_ENV: "https://your-polygon-rpc-endpoint"})
    assert "your-polygon" not in str(caught.value)


def test_rpc_url_must_be_http_and_the_error_hides_the_value() -> None:
    with pytest.raises(PolygonRpcError, match="polygon_rpc_url_scheme") as caught:
        payout_caller_from_env({POLYGON_RPC_ENV: "file:///tmp/secret-endpoint"})
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_chain_must_be_polygon_before_a_payout_call() -> None:
    calls: list[str] = []

    async def poster(method: str, params: list) -> tuple[int, dict]:
        calls.append(method)
        assert params == [] or params[1] == "latest"
        if method == "eth_chainId":
            return _ok("0x1")
        raise AssertionError("eth_call must not run on the wrong chain")

    reader = PolygonRpcReader("https://rpc.example", poster=poster, attempts=3)
    with pytest.raises(PolygonRpcError, match="polygon_rpc_wrong_chain"):
        await reader.eth_call(to=CONDITIONAL_TOKENS_ADDRESS, data="0xdd34de67")
    assert calls == ["eth_chainId"]


@pytest.mark.asyncio
async def test_timeout_retries_then_fails_closed() -> None:
    calls = 0

    async def poster(method: str, params: list) -> tuple[int, dict]:
        nonlocal calls
        calls += 1
        raise TimeoutError("stall")

    reader = PolygonRpcReader(
        "https://rpc.example", poster=poster, attempts=2, retry_delay_s=0
    )
    with pytest.raises(PolygonRpcError, match="polygon_rpc_timeout"):
        await reader.eth_call(to=CONDITIONAL_TOKENS_ADDRESS, data="0xabc")
    assert calls == 2


@pytest.mark.asyncio
async def test_transient_http_is_retried_then_a_payout_is_read() -> None:
    calls: list[tuple[str, list]] = []

    async def poster(method: str, params: list) -> tuple[int, dict]:
        calls.append((method, params))
        if method == "eth_chainId":
            return _ok("0x89")
        if method == "eth_call" and len(calls) == 2:
            return 429, {"jsonrpc": "2.0", "id": 1, "error": {"code": 429, "message": "rate limit"}}
        word = params[0]["data"]
        if word.startswith("0x" + PAYOUT_DENOMINATOR_SELECTOR.hex()):
            return _ok("0x" + format(1, "064x"))
        return _ok("0x" + format(1, "064x"))

    reader = PolygonRpcReader(
        "https://rpc.example", poster=poster, attempts=3, retry_delay_s=0
    )
    payout = await read_binary_payout(
        reader.eth_call, condition_id=CONDITION, outcome_index=0
    )
    assert payout == Decimal("1")
    assert calls[0][0] == "eth_chainId"
    assert calls[1][0] == "eth_call"
    assert calls[1][1][1] == "latest"
    assert calls[1][1][0]["to"] == CONDITIONAL_TOKENS_ADDRESS
    assert any(item[0] == "eth_call" and reader._chain_ok for item in calls[2:])


@pytest.mark.asyncio
async def test_exhausted_retry_stays_unresolved() -> None:
    async def poster(method: str, params: list) -> tuple[int, dict]:
        if method == "eth_chainId":
            return _ok("0x89")
        return 503, None

    reader = PolygonRpcReader(
        "https://rpc.example", poster=poster, attempts=2, retry_delay_s=0
    )
    with pytest.raises(PolygonRpcError, match="polygon_rpc_retry"):
        await reader.eth_call(to=CONDITIONAL_TOKENS_ADDRESS, data="0x" + "ab" * 4)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"jsonrpc": "2.0", "id": 2, "result": "0x" + "00" * 32},
        {"id": 1, "result": "0x" + "00" * 32},
        {"jsonrpc": "1.0", "id": 1, "result": "0x" + "00" * 32},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": "0x" + "00" * 32,
            "error": {"code": -32000, "message": "mixed"},
        },
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "result": "0x1234"},
        {"jsonrpc": "2.0", "id": 1, "result": 137},
    ],
)
async def test_malformed_envelopes_are_rejected(body: dict) -> None:
    async def poster(method: str, params: list) -> tuple[int, dict]:
        if method == "eth_chainId":
            return _ok("0x89")
        return 200, body

    reader = PolygonRpcReader("https://rpc.example", poster=poster, attempts=2)
    with pytest.raises(PolygonRpcError, match="polygon_rpc_malformed"):
        await reader.eth_call(to=CONDITIONAL_TOKENS_ADDRESS, data="0xabcd")


@pytest.mark.asyncio
async def test_valid_polygon_chain_and_payout_word() -> None:
    async def poster(method: str, params: list) -> tuple[int, dict]:
        if method == "eth_chainId":
            return _ok("0x89")
        assert params[1] == "latest"
        return _ok("0x" + format(1, "064x"))

    reader = PolygonRpcReader("https://rpc.example", poster=poster, attempts=1)
    payout = await read_binary_payout(reader.eth_call, condition_id=CONDITION, outcome_index=0)
    assert payout == Decimal("1")


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


@pytest.mark.asyncio
async def test_real_transport_retries_a_transient_status_then_reads_chain() -> None:
    class Handler(BaseHTTPRequestHandler):
        calls = 0

        def do_POST(self) -> None:  # noqa: N802
            Handler.calls += 1
            length = int(self.headers.get("Content-Length") or "0")
            self.rfile.read(length)
            if Handler.calls == 1:
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "error": {"code": 429, "message": "rate limit"}}
                ).encode()
                self.send_response(429)
            else:
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0x89"}).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    server, url = _serve(Handler)
    try:
        reader = PolygonRpcReader(url, attempts=2, retry_delay_s=0, timeout_s=2)
        await reader._ensure_chain()
        assert reader._chain_ok is True
        assert Handler.calls == 2
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_real_transport_timeout_is_bounded() -> None:
    reader = PolygonRpcReader(
        "http://127.0.0.1:1",
        attempts=2,
        retry_delay_s=0,
        timeout_s=0.2,
    )
    with pytest.raises(PolygonRpcError, match="polygon_rpc_timeout"):
        await reader._ensure_chain()


@pytest.mark.asyncio
async def test_invalid_json_rpc_body_is_not_a_payout() -> None:
    async def poster(method: str, params: list) -> tuple[int, dict]:
        if method == "eth_chainId":
            return _ok("0x89")
        return 200, ["not", "an", "object"]

    reader = PolygonRpcReader("https://rpc.example", poster=poster, attempts=2)
    with pytest.raises(PolygonRpcError, match="polygon_rpc_malformed"):
        await reader.eth_call(to=CONDITIONAL_TOKENS_ADDRESS, data="0xabcd")
