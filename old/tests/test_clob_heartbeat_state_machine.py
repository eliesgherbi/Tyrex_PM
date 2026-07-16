"""Polymarket CLOB heartbeat session: first POST \"\", then server-provided id; 400 rotates id."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from py_clob_client_v2.exceptions import PolyApiException

from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.venue.polymarket.clob_heartbeat import (
    HEARTBEAT_RECOVER_MAX_ATTEMPTS,
    parse_heartbeat_id_from_error_body,
    parse_heartbeat_id_from_success_body,
    post_heartbeat_with_recovery,
)


def test_parse_success_body_extracts_id() -> None:
    sid = "aaa111bbb222ccc333ddd444eee555fff"
    assert parse_heartbeat_id_from_success_body({"status": "ok", "heartbeat_id": sid}) == sid
    assert parse_heartbeat_id_from_success_body({"status": "ok"}) is None


def test_parse_error_body_extracts_id() -> None:
    assert (
        parse_heartbeat_id_from_error_body(
            {"heartbeat_id": "bb11223344556677889900aabbccddeeff", "error_msg": "Invalid Heartbeat ID"}
        )
        == "bb11223344556677889900aabbccddeeff"
    )


def test_parse_error_body_heartbeat_id_camel_case() -> None:
    assert (
        parse_heartbeat_id_from_error_body({"heartbeatId": "cc00112233445566778899aabbccddeeff", "error": "bad"})
        == "cc00112233445566778899aabbccddeeff"
    )


def test_parse_success_nested_data() -> None:
    sid = "dd00112233445566778899aabbccddeeff"
    assert parse_heartbeat_id_from_success_body({"status": "ok", "data": {"heartbeat_id": sid}}) == sid


def test_parse_preserves_hyphenated_uuid_verbatim() -> None:
    u = "cad758a9-b067-48e1-b0b7-383a576d9252"
    assert parse_heartbeat_id_from_success_body({"status": "ok", "heartbeat_id": u}) == u


@pytest.mark.asyncio
async def test_first_post_empty_then_second_uses_id_from_200() -> None:
    health = HealthRuntime()
    bridge = MagicMock()
    sid = "session111222333444555666778899aabbccddeeff"
    bridge.post_heartbeat = AsyncMock(
        side_effect=[
            {"status": "ok", "heartbeat_id": sid},
            {"status": "ok"},
        ]
    )
    assert await post_heartbeat_with_recovery(health, bridge)
    bridge.post_heartbeat.assert_called_with("")
    assert health.clob_heartbeat_id_next == sid

    assert await post_heartbeat_with_recovery(health, bridge)
    assert bridge.post_heartbeat.call_args_list[1].args[0] == sid


@pytest.mark.asyncio
async def test_400_with_replacement_id_retries_same_tick() -> None:
    health = HealthRuntime()
    bridge = MagicMock()
    repl = "replacement11223344556677889900aabbccddeeff"
    resp400 = httpx.Response(
        400,
        json={"heartbeat_id": repl, "error_msg": "Invalid Heartbeat ID"},
    )
    exc = PolyApiException(resp400)
    bridge.post_heartbeat = AsyncMock(
        side_effect=[
            exc,
            {"status": "ok", "heartbeat_id": repl},
        ]
    )
    assert await post_heartbeat_with_recovery(health, bridge)
    assert bridge.post_heartbeat.call_count == 2
    assert bridge.post_heartbeat.call_args_list[0].args[0] == ""
    assert bridge.post_heartbeat.call_args_list[1].args[0] == repl
    assert health.clob_heartbeat_id_next == repl


@pytest.mark.asyncio
async def test_repeated_ticks_use_stable_session_id() -> None:
    health = HealthRuntime()
    bridge = MagicMock()
    sid = "stable00112233445566778899aabbccddeeff"
    bridge.post_heartbeat = AsyncMock(return_value={"status": "ok", "heartbeat_id": sid})
    for _ in range(3):
        assert await post_heartbeat_with_recovery(health, bridge)
    assert bridge.post_heartbeat.call_count == 3
    assert bridge.post_heartbeat.call_args_list[0].args[0] == ""
    assert bridge.post_heartbeat.call_args_list[1].args[0] == sid
    assert bridge.post_heartbeat.call_args_list[2].args[0] == sid


@pytest.mark.asyncio
async def test_non_400_poly_error_fails_without_retry() -> None:
    health = HealthRuntime()
    bridge = MagicMock()
    resp401 = httpx.Response(401, json={"error": "nope"})
    bridge.post_heartbeat = AsyncMock(side_effect=PolyApiException(resp401))
    assert not await post_heartbeat_with_recovery(health, bridge)
    assert bridge.post_heartbeat.call_count == 1


@pytest.mark.asyncio
async def test_openapi_style_200_ok_only_then_400_supplies_id_next_ticks_use_it() -> None:
    """Production often omits heartbeat_id on 200; client must recover via 400 then keep that id."""
    health = HealthRuntime()
    bridge = MagicMock()
    sid = "venue-session-aa00112233445566778899aabbccddeeff"
    exc = PolyApiException(
        httpx.Response(400, json={"heartbeatId": sid, "error_msg": "Invalid Heartbeat ID"})
    )
    bridge.post_heartbeat = AsyncMock(
        side_effect=[
            {"status": "ok"},
            exc,
            {"status": "ok"},
            {"status": "ok"},
        ]
    )
    assert await post_heartbeat_with_recovery(health, bridge)
    assert health.clob_heartbeat_id_next is None

    assert await post_heartbeat_with_recovery(health, bridge)
    assert bridge.post_heartbeat.call_args_list[1].args[0] == ""
    assert bridge.post_heartbeat.call_args_list[2].args[0] == sid
    assert health.clob_heartbeat_id_next == sid

    assert await post_heartbeat_with_recovery(health, bridge)
    assert bridge.post_heartbeat.call_args_list[3].args[0] == sid


@pytest.mark.asyncio
async def test_exhausted_recovery_clears_stored_id() -> None:
    health = HealthRuntime()
    bridge = MagicMock()
    resp400 = httpx.Response(
        400,
        json={"heartbeat_id": "deadbeef", "error_msg": "Invalid Heartbeat ID"},
    )
    bridge.post_heartbeat = AsyncMock(side_effect=PolyApiException(resp400))
    assert not await post_heartbeat_with_recovery(health, bridge)
    assert bridge.post_heartbeat.call_count == HEARTBEAT_RECOVER_MAX_ATTEMPTS
    assert health.clob_heartbeat_id_next is None


@pytest.mark.asyncio
async def test_concurrent_post_heartbeat_calls_are_serialized() -> None:
    health = HealthRuntime()
    bridge = MagicMock()
    inflight = 0
    max_inf = 0

    async def post(_hid: str) -> dict:
        nonlocal inflight, max_inf
        inflight += 1
        max_inf = max(max_inf, inflight)
        await asyncio.sleep(0.02)
        inflight -= 1
        return {"status": "ok", "heartbeat_id": "aa00112233445566778899aabbccddeeff"}

    bridge.post_heartbeat = AsyncMock(side_effect=post)
    await asyncio.gather(*(post_heartbeat_with_recovery(health, bridge) for _ in range(5)))
    assert max_inf == 1
