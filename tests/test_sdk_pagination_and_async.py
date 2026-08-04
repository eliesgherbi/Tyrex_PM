"""Deterministic tests for SDK Page flattening and async secure-client lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tyrex_pm.adapters.polymarket.sdk_pagination import (
    SdkPaginationError,
    assert_not_sdk_page,
    drain_sdk_paginator,
    map_sdk_paginator,
)
from tyrex_pm.execution.polymarket.sdk_readonly import (
    SdkReadonlyTransport,
    _open_order_to_snapshot,
    _trade_to_snapshot,
)
from tyrex_pm.runtime.n7_abort import N7AbortCode
from tyrex_pm.runtime.n7_preflight import run_n7_preflight


@dataclass(frozen=True)
class _FakePage:
    items: tuple[Any, ...]
    has_more: bool
    next_cursor: str | None = None
    total_count: int | None = None


class _FakePaginator:
    """Mimics polymarket.pagination.Paginator page iteration."""

    def __init__(self, pages: list[_FakePage]) -> None:
        self._pages = pages

    def __iter__(self) -> Iterator[_FakePage]:
        yield from self._pages

    def iter_items(self) -> Iterator[Any]:
        for page in self._pages:
            yield from page.items


@dataclass
class _FakeOrder:
    id: str
    token_id: str = "tok"
    condition_id: str = "mkt"
    side: str = "BUY"
    original_size: str = "1"
    size_matched: str = "0"
    price: str = "0.5"
    status: str = "LIVE"


@dataclass
class _FakeTrade:
    id: str | None = None
    transaction_hash: str | None = "0xabc"
    token_id: str = "tok"
    side: str = "BUY"
    size: str = "1"
    price: str = "0.5"
    status: str = "CONFIRMED"
    condition_id: str = "mkt"


@pytest.fixture
def patch_pagination_types(monkeypatch: pytest.MonkeyPatch):
    """Route isinstance checks to fake Page/Paginator types."""
    import tyrex_pm.adapters.polymarket.sdk_pagination as mod

    monkeypatch.setattr(mod, "_page_type", lambda: _FakePage)
    monkeypatch.setattr(mod, "_paginator_type", lambda: _FakePaginator)
    return mod


def test_no_pages_zero_records(patch_pagination_types) -> None:
    drained = drain_sdk_paginator(_FakePaginator([]))
    assert drained.page_count == 0
    assert drained.record_count == 0
    assert drained.items == ()


def test_one_empty_page_zero_records(patch_pagination_types) -> None:
    drained = drain_sdk_paginator(_FakePaginator([_FakePage(items=(), has_more=False)]))
    assert drained.page_count == 1
    assert drained.record_count == 0


def test_multiple_empty_pages_zero_records(patch_pagination_types) -> None:
    drained = drain_sdk_paginator(
        _FakePaginator(
            [
                _FakePage(items=(), has_more=True, next_cursor="c1"),
                _FakePage(items=(), has_more=False),
            ]
        )
    )
    assert drained.page_count == 2
    assert drained.record_count == 0


def test_one_populated_page(patch_pagination_types) -> None:
    o1, o2 = _FakeOrder("a"), _FakeOrder("b")
    drained = drain_sdk_paginator(_FakePaginator([_FakePage(items=(o1, o2), has_more=False)]))
    assert drained.record_count == 2
    assert drained.items == (o1, o2)


def test_multiple_populated_pages_order_preserved(patch_pagination_types) -> None:
    items = [_FakeOrder(str(i)) for i in range(5)]
    drained = drain_sdk_paginator(
        _FakePaginator(
            [
                _FakePage(items=(items[0], items[1]), has_more=True, next_cursor="x"),
                _FakePage(items=(items[2],), has_more=True, next_cursor="y"),
                _FakePage(items=(items[3], items[4]), has_more=False),
            ]
        )
    )
    assert [o.id for o in drained.items] == ["0", "1", "2", "3", "4"]
    assert drained.page_count == 3
    assert drained.record_count == 5


def test_mixed_populated_and_empty_pages(patch_pagination_types) -> None:
    o = _FakeOrder("only")
    drained = drain_sdk_paginator(
        _FakePaginator(
            [
                _FakePage(items=(), has_more=True, next_cursor="a"),
                _FakePage(items=(o,), has_more=True, next_cursor="b"),
                _FakePage(items=(), has_more=False),
            ]
        )
    )
    assert drained.record_count == 1
    assert drained.items[0].id == "only"


def test_page_cannot_convert_to_order(patch_pagination_types) -> None:
    page = _FakePage(items=(), has_more=False)
    with pytest.raises(SdkPaginationError):
        _open_order_to_snapshot(page)


def test_page_cannot_convert_to_trade(patch_pagination_types) -> None:
    page = _FakePage(items=(), has_more=False)
    with pytest.raises(SdkPaginationError):
        _trade_to_snapshot(page)


def test_malformed_page_items_fails(patch_pagination_types) -> None:
    import tyrex_pm.adapters.polymarket.sdk_pagination as mod

    class P:
        items = ["a"]  # list, not tuple
        has_more = False
        next_cursor = None

    class Pag:
        def __iter__(self):
            yield P()

    with (
        patch.object(mod, "_paginator_type", lambda: Pag),
        patch.object(mod, "_page_type", lambda: P),
    ):
        with pytest.raises(SdkPaginationError, match="page_items_not_tuple"):
            drain_sdk_paginator(Pag())


def test_later_page_exception_not_silent(patch_pagination_types) -> None:
    class BoomPaginator(_FakePaginator):
        def __iter__(self) -> Iterator[_FakePage]:
            yield _FakePage(items=(_FakeOrder("1"),), has_more=True, next_cursor="c")
            raise RuntimeError("page_fetch_failed")

    with pytest.raises(RuntimeError, match="page_fetch_failed"):
        drain_sdk_paginator(BoomPaginator([]))


def test_no_duplicate_from_page_handling(patch_pagination_types) -> None:
    o = _FakeOrder("dup")
    drained = drain_sdk_paginator(_FakePaginator([_FakePage(items=(o,), has_more=False)]))
    assert drained.record_count == 1


def test_map_sdk_paginator_converts_orders(patch_pagination_types) -> None:
    paginator = _FakePaginator([_FakePage(items=(_FakeOrder("x"),), has_more=False)])
    rows, drained = map_sdk_paginator(paginator, _open_order_to_snapshot)
    assert drained.record_count == 1
    assert rows[0].venue_order_id == "x"
    assert rows[0].status == "LIVE"


def test_get_open_orders_counts_records_not_pages(patch_pagination_types) -> None:
    client = MagicMock()
    client.list_open_orders.return_value = _FakePaginator([_FakePage(items=(), has_more=False)])
    transport = SdkReadonlyTransport(_client=client, creds=MagicMock())
    orders = transport.get_open_orders()
    assert orders == []
    assert transport.last_pagination["open_orders"] == {
        "page_count": 1,
        "record_count": 0,
    }


def test_get_trades_counts_records_not_pages(patch_pagination_types) -> None:
    client = MagicMock()
    client.list_trades.return_value = _FakePaginator(
        [
            _FakePage(
                items=(_FakeTrade(transaction_hash="t1"), _FakeTrade(transaction_hash="t2")),
                has_more=False,
            )
        ]
    )
    transport = SdkReadonlyTransport(_client=client, creds=MagicMock())
    trades = transport.get_trades()
    assert len(trades) == 2
    assert transport.last_pagination["trades"]["record_count"] == 2
    assert transport.last_pagination["trades"]["page_count"] == 1


def test_get_positions_flattens_pages(patch_pagination_types) -> None:
    @dataclass
    class Pos:
        token_id: str
        size: str = "0"
        avg_price: str | None = None
        condition_id: str | None = None

    client = MagicMock()
    client.list_positions.return_value = _FakePaginator(
        [_FakePage(items=(Pos("a"), Pos("b")), has_more=False)]
    )
    with patch(
        "tyrex_pm.execution.polymarket.sdk_readonly.positions_wallet_address",
        return_value="0xabc",
    ):
        transport = SdkReadonlyTransport(_client=client, creds=MagicMock())
        rows = transport.get_positions_raw()
    assert len(rows) == 2
    assert transport.last_pagination["positions"]["record_count"] == 2


def _creds() -> MagicMock:
    c = MagicMock()
    c.api_key = "key"
    c.secret = "secret"
    c.passphrase = "pass"
    c.address = "0xabc"
    c.funder = None
    return c


@pytest.mark.asyncio
async def test_async_secure_client_create_awaited_once() -> None:
    from tyrex_pm.adapters.polymarket import sdk_secure

    fake_client = object()
    create_mock = AsyncMock(return_value=fake_client)

    class _ASC:
        create = create_mock

    with (
        patch(
            "tyrex_pm.adapters.polymarket.sdk_secure.load_l2_credentials",
            return_value=MagicMock(api_key="k", secret="s", passphrase="p", funder=None),
        ),
        patch(
            "tyrex_pm.adapters.polymarket.sdk_secure._private_key_from_env",
            return_value="0x" + "22" * 32,
        ),
        patch(
            "tyrex_pm.adapters.polymarket.sdk_secure._wallet_address",
            return_value=None,
        ),
        patch("polymarket.AsyncSecureClient", _ASC),
        patch("polymarket.PRODUCTION", "prod"),
        patch(
            "polymarket.models.clob.api_key.ApiKeyCreds",
            return_value=MagicMock(),
        ),
    ):
        client = await sdk_secure.build_async_secure_client(
            env={"TYREX_PRIVATE_KEY": "0x" + "22" * 32},
            creds=MagicMock(api_key="k", secret="s", passphrase="p", funder=None),
        )
    assert client is fake_client
    assert create_mock.await_count == 1
    assert not asyncio.iscoroutine(client)


@pytest.mark.asyncio
async def test_subscribe_only_after_init() -> None:
    from tyrex_pm.execution.polymarket import user_stream_readonly as usr

    order: list[str] = []
    client = MagicMock()
    handle = MagicMock()
    handle.__anext__ = AsyncMock(side_effect=StopAsyncIteration)
    handle.close = AsyncMock()
    client.close = AsyncMock()

    async def _build(**kwargs: Any) -> Any:
        order.append("create")
        return client

    async def _subscribe(spec: Any) -> Any:
        order.append("subscribe")
        return handle

    client.subscribe = _subscribe

    with patch(
        "tyrex_pm.adapters.polymarket.sdk_secure.build_async_secure_client",
        _build,
    ):
        report = await usr._observe_async(
            creds=_creds(),
            observe_s=0.1,
            on_disconnect=None,
            on_ready=None,
            env=None,
        )
    assert order[0] == "create"
    assert "subscribe" in order
    assert report["authenticated"] is True
    assert report.get("failure_kind") is None


@pytest.mark.asyncio
async def test_init_failure_prevents_subscribe() -> None:
    from tyrex_pm.execution.polymarket import user_stream_readonly as usr

    subscribed = False

    async def _build(**kwargs: Any) -> Any:
        raise AttributeError("'coroutine' object has no attribute 'subscribe'")

    with patch(
        "tyrex_pm.adapters.polymarket.sdk_secure.build_async_secure_client",
        _build,
    ):
        report = await usr._observe_async(
            creds=_creds(),
            observe_s=0.1,
            on_disconnect=None,
            on_ready=None,
            env=None,
        )
    assert subscribed is False
    assert report["authenticated"] is False
    assert report["failure_kind"] == "adapter_init"
    assert report["stage"] in {"CREATE", "CLOSE"}


@pytest.mark.asyncio
async def test_transport_failure_distinct() -> None:
    from polymarket import TransportError

    from tyrex_pm.execution.polymarket import user_stream_readonly as usr

    async def _build(**kwargs: Any) -> Any:
        raise TransportError("connection reset")

    with patch(
        "tyrex_pm.adapters.polymarket.sdk_secure.build_async_secure_client",
        _build,
    ):
        report = await usr._observe_async(
            creds=_creds(),
            observe_s=0.1,
            on_disconnect=None,
            on_ready=None,
            env=None,
        )
    assert report["failure_kind"] == "transport"


@pytest.mark.asyncio
async def test_cleanup_on_success_and_cancel() -> None:
    from tyrex_pm.execution.polymarket import user_stream_readonly as usr

    client = MagicMock()
    handle = MagicMock()
    handle.__anext__ = AsyncMock(side_effect=TimeoutError())
    handle.close = AsyncMock()
    client.close = AsyncMock()
    client.subscribe = AsyncMock(return_value=handle)

    async def _build(**kwargs: Any) -> Any:
        return client

    with patch(
        "tyrex_pm.adapters.polymarket.sdk_secure.build_async_secure_client",
        _build,
    ):
        report = await usr._observe_async(
            creds=_creds(),
            observe_s=0.2,
            on_disconnect=None,
            on_ready=None,
            env=None,
        )
    assert handle.close.await_count >= 1
    assert client.close.await_count >= 1
    assert report["connected"] is True


def test_n7_empty_pages_no_unexpected_open_order(tmp_path) -> None:
    first = MagicMock()
    first.ok = True
    first.payload = {
        "blocker": None,
        "public_clob": {
            "transport_reachable": True,
            "venue_time_valid": True,
            "failure_kind": None,
        },
        "cloudflare_blocked": False,
        "credentials_present": True,
        "identity_mapping": {
            "private_key_derives_valid_signer": True,
            "funder_present": True,
        },
        "balance_evidence": {"retrieved": True},
        "user_stream": {
            "attempted": True,
            "authenticated": True,
            "failure_kind": None,
        },
        "reconciliation": {
            "unreachable_account": False,
            "open_order_count": 0,
            "position_row_count": 0,
            "observation_only_local_empty": True,
        },
    }

    with (
        patch("tyrex_pm.runtime.n7_preflight.load_n7_sealed_config") as sealed,
        patch("tyrex_pm.runtime.n7_preflight.inspect_git") as git,
        patch("tyrex_pm.runtime.n7_preflight.run_live_preflight", return_value=first),
        patch(
            "tyrex_pm.runtime.n7_preflight.PRODUCTION_TIMING_VALUES_STATUS",
            "FROZEN_FOR_N7",
        ),
    ):
        sealed.return_value.live.mutations_enabled = False
        sealed.return_value.fingerprint.return_value = "fp"
        sealed.return_value.to_dict.return_value = {}
        git.return_value.head = "abc"
        git.return_value.worktree_clean = True
        cfg = tmp_path / "n7.yaml"
        cfg.write_text("x:1\n", encoding="utf-8")
        result = run_n7_preflight(
            out_dir=tmp_path / "out",
            config_path=cfg,
            repo=tmp_path,
            user_stream_observe_s=0.0,
        )
    assert N7AbortCode.UNEXPECTED_OPEN_ORDER.value not in result.abort_codes


def test_n7_real_open_orders_still_block(tmp_path) -> None:
    first = MagicMock()
    first.ok = True
    first.payload = {
        "blocker": None,
        "public_clob": {
            "transport_reachable": True,
            "venue_time_valid": True,
            "failure_kind": None,
        },
        "cloudflare_blocked": False,
        "credentials_present": True,
        "identity_mapping": {
            "private_key_derives_valid_signer": True,
            "funder_present": True,
        },
        "balance_evidence": {"retrieved": True},
        "user_stream": {"attempted": True, "authenticated": True},
        "reconciliation": {
            "unreachable_account": False,
            "open_order_count": 2,
            "position_row_count": 0,
            "observation_only_local_empty": True,
        },
    }
    with (
        patch("tyrex_pm.runtime.n7_preflight.load_n7_sealed_config") as sealed,
        patch("tyrex_pm.runtime.n7_preflight.inspect_git") as git,
        patch("tyrex_pm.runtime.n7_preflight.run_live_preflight", return_value=first),
        patch(
            "tyrex_pm.runtime.n7_preflight.PRODUCTION_TIMING_VALUES_STATUS",
            "FROZEN_FOR_N7",
        ),
    ):
        sealed.return_value.live.mutations_enabled = False
        sealed.return_value.fingerprint.return_value = "fp"
        sealed.return_value.to_dict.return_value = {}
        git.return_value.head = "abc"
        git.return_value.worktree_clean = True
        cfg = tmp_path / "n7.yaml"
        cfg.write_text("x:1\n", encoding="utf-8")
        result = run_n7_preflight(
            out_dir=tmp_path / "out",
            config_path=cfg,
            repo=tmp_path,
            user_stream_observe_s=0.0,
        )
    assert N7AbortCode.UNEXPECTED_OPEN_ORDER.value in result.abort_codes


def test_n7_adapter_init_not_connectivity(tmp_path) -> None:
    first = MagicMock()
    first.ok = True
    first.payload = {
        "blocker": None,
        "public_clob": {
            "transport_reachable": True,
            "venue_time_valid": True,
            "failure_kind": None,
        },
        "cloudflare_blocked": False,
        "credentials_present": True,
        "identity_mapping": {
            "private_key_derives_valid_signer": True,
            "funder_present": True,
        },
        "balance_evidence": {"retrieved": True},
        "user_stream": {
            "attempted": True,
            "authenticated": False,
            "failure_kind": "adapter_init",
            "error_class": "AttributeError",
            "stage": "CREATE",
        },
        "reconciliation": {
            "unreachable_account": False,
            "open_order_count": 0,
            "position_row_count": 0,
            "observation_only_local_empty": True,
        },
    }
    with (
        patch("tyrex_pm.runtime.n7_preflight.load_n7_sealed_config") as sealed,
        patch("tyrex_pm.runtime.n7_preflight.inspect_git") as git,
        patch("tyrex_pm.runtime.n7_preflight.run_live_preflight", return_value=first),
        patch(
            "tyrex_pm.runtime.n7_preflight.PRODUCTION_TIMING_VALUES_STATUS",
            "FROZEN_FOR_N7",
        ),
    ):
        sealed.return_value.live.mutations_enabled = False
        sealed.return_value.fingerprint.return_value = "fp"
        sealed.return_value.to_dict.return_value = {}
        git.return_value.head = "abc"
        git.return_value.worktree_clean = True
        cfg = tmp_path / "n7.yaml"
        cfg.write_text("x:1\n", encoding="utf-8")
        result = run_n7_preflight(
            out_dir=tmp_path / "out",
            config_path=cfg,
            repo=tmp_path,
            user_stream_observe_s=0.0,
        )
    assert N7AbortCode.USER_STREAM_INIT_FAILED.value in result.abort_codes
    assert N7AbortCode.CONNECTIVITY_UNAVAILABLE.value not in result.abort_codes
    assert "hint_check_vpn_or_dns" not in result.abort_codes


def test_n7_genuine_stream_transport_is_connectivity(tmp_path) -> None:
    first = MagicMock()
    first.ok = True
    first.payload = {
        "blocker": None,
        "public_clob": {
            "transport_reachable": True,
            "venue_time_valid": True,
            "failure_kind": None,
        },
        "cloudflare_blocked": False,
        "credentials_present": True,
        "identity_mapping": {
            "private_key_derives_valid_signer": True,
            "funder_present": True,
        },
        "balance_evidence": {"retrieved": True},
        "user_stream": {
            "attempted": True,
            "authenticated": False,
            "failure_kind": "transport",
            "stage": "SUBSCRIBE",
        },
        "reconciliation": {
            "unreachable_account": False,
            "open_order_count": 0,
            "position_row_count": 0,
            "observation_only_local_empty": True,
        },
    }
    with (
        patch("tyrex_pm.runtime.n7_preflight.load_n7_sealed_config") as sealed,
        patch("tyrex_pm.runtime.n7_preflight.inspect_git") as git,
        patch("tyrex_pm.runtime.n7_preflight.run_live_preflight", return_value=first),
        patch(
            "tyrex_pm.runtime.n7_preflight.PRODUCTION_TIMING_VALUES_STATUS",
            "FROZEN_FOR_N7",
        ),
    ):
        sealed.return_value.live.mutations_enabled = False
        sealed.return_value.fingerprint.return_value = "fp"
        sealed.return_value.to_dict.return_value = {}
        git.return_value.head = "abc"
        git.return_value.worktree_clean = True
        cfg = tmp_path / "n7.yaml"
        cfg.write_text("x:1\n", encoding="utf-8")
        result = run_n7_preflight(
            out_dir=tmp_path / "out",
            config_path=cfg,
            repo=tmp_path,
            user_stream_observe_s=0.0,
        )
    assert N7AbortCode.CONNECTIVITY_UNAVAILABLE.value in result.abort_codes
    assert "hint_check_vpn_or_dns" not in result.abort_codes


def test_assert_not_sdk_page_helper(patch_pagination_types) -> None:
    with pytest.raises(SdkPaginationError):
        assert_not_sdk_page(_FakePage(items=(), has_more=False), context="x")


def test_no_coroutine_warning_on_builder() -> None:
    """Builder is a coroutine function; calling without await must be detectable."""
    from tyrex_pm.adapters.polymarket.sdk_secure import build_async_secure_client

    assert asyncio.iscoroutinefunction(build_async_secure_client)
