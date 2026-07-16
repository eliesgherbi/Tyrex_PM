"""Positions REST safety net (data-api/positions).

In LIVE mode, ``WalletStore.positions`` historically only updated on user-WS TRADE events
with status="CONFIRMED". A WS reconnect that dropped a CONFIRMED message — or a manual
UI action completing before WS hydration — would leave the bot with a stale or empty
positions view, and downstream deployment-cap evaluation would silently misbehave.

These tests cover the REST refresh path:

  * ``normalize_position_rows`` parses the documented ``asset``/``size``/``avgPrice``
    schema as well as the historical ``token_id``/``qty``/``avg_price`` aliases.
  * ``refresh_positions_into_wallet`` replaces ``WalletStore.positions`` wholesale and
    stamps ``last_positions_sync_ts`` so downstream readiness checks can see freshness.
  * ``refresh_positions_from_data_api`` is fail-soft: a transport error is logged and
    leaves the prior snapshot untouched (the next tick retries).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.venue.polymarket.positions_sync import (
    normalize_position_rows,
    refresh_positions_from_data_api,
    refresh_positions_into_wallet,
)


def test_normalize_documented_schema() -> None:
    rows = [
        {"asset": "111", "size": "10.5", "avgPrice": "0.42"},
        {"asset": "222", "size": "0", "avgPrice": "0.5"},  # zero size dropped
        {"asset": "333", "size": "7", "avgPrice": "0"},  # zero avg => no mark
    ]
    out = normalize_position_rows(rows)
    assert set(out.keys()) == {TokenId("111"), TokenId("333")}
    assert out[TokenId("111")].qty == Decimal("10.5")
    assert out[TokenId("111")].avg_price_usd == Decimal("0.42")
    assert out[TokenId("333")].qty == Decimal("7")
    assert out[TokenId("333")].avg_price_usd is None


def test_normalize_historical_aliases() -> None:
    rows = [{"token_id": "777", "qty": "3", "avg_price": "0.25"}]
    out = normalize_position_rows(rows)
    assert out[TokenId("777")].qty == Decimal("3")
    assert out[TokenId("777")].avg_price_usd == Decimal("0.25")


def test_normalize_skips_garbage_rows() -> None:
    rows = [None, "string", {"size": "10"}, {"asset": "x", "size": "abc"}]
    assert normalize_position_rows([r for r in rows if r is not None]) == {}  # type: ignore[arg-type]


def test_refresh_positions_into_wallet_replaces_and_stamps() -> None:
    w = WalletStore()
    w.positions[TokenId("ghost")] = WalletPosition(
        token_id=TokenId("ghost"), qty=Decimal("-5"), avg_price_usd=None
    )
    rows = [{"asset": "real", "size": "8", "avgPrice": "0.7"}]
    new = refresh_positions_into_wallet(w, rows)
    assert TokenId("ghost") not in w.positions, "REST snapshot must clear stale ghosts"
    assert new[TokenId("real")].qty == Decimal("8")
    assert w.last_positions_sync_ts is not None


class _StubClient:
    """Minimal DataApiClient stand-in driven by an injected payload."""

    def __init__(self, rows: list[dict[str, Any]] | Exception) -> None:
        self._rows = rows
        self.calls: list[str] = []

    async def fetch_positions(self, wallet_address: str) -> list[dict[str, Any]]:
        self.calls.append(wallet_address)
        if isinstance(self._rows, Exception):
            raise self._rows
        return self._rows


def test_refresh_positions_from_data_api_success() -> None:
    w = WalletStore()
    client = _StubClient([{"asset": "tok", "size": "12", "avgPrice": "0.33"}])
    ok = asyncio.run(refresh_positions_from_data_api(w, client, "0xwallet"))  # type: ignore[arg-type]
    assert ok is True
    assert w.positions[TokenId("tok")].qty == Decimal("12")
    assert client.calls == ["0xwallet"]


def test_refresh_positions_from_data_api_failure_preserves_state() -> None:
    w = WalletStore()
    w.positions[TokenId("keep")] = WalletPosition(
        token_id=TokenId("keep"), qty=Decimal("4"), avg_price_usd=Decimal("0.5")
    )
    client = _StubClient(RuntimeError("network down"))
    ok = asyncio.run(refresh_positions_from_data_api(w, client, "0xwallet"))  # type: ignore[arg-type]
    assert ok is False
    assert w.positions[TokenId("keep")].qty == Decimal("4"), "prior snapshot must survive transport failure"


def test_refresh_positions_skips_when_address_missing() -> None:
    w = WalletStore()
    client = _StubClient([])
    ok = asyncio.run(refresh_positions_from_data_api(w, client, ""))  # type: ignore[arg-type]
    assert ok is False
    assert client.calls == []


@pytest.mark.parametrize("address_env", ["TYREX_FUNDER", "POLYMARKET_FUNDER"])
def test_resolve_positions_wallet_address_prefers_funder(monkeypatch, address_env: str) -> None:
    """Funder env wins over the EOA — positions on Polymarket sit on the proxy contract."""
    from tyrex_pm.venue.polymarket.clob_env import resolve_positions_wallet_address

    monkeypatch.delenv("TYREX_FUNDER", raising=False)
    monkeypatch.delenv("POLYMARKET_FUNDER", raising=False)
    monkeypatch.setenv(address_env, "0xfunder")

    class FakeClient:
        def get_address(self) -> str:
            return "0xeoa"

    assert resolve_positions_wallet_address(FakeClient()) == "0xfunder"


def test_resolve_positions_wallet_address_falls_back_to_eoa(monkeypatch) -> None:
    from tyrex_pm.venue.polymarket.clob_env import resolve_positions_wallet_address

    monkeypatch.delenv("TYREX_FUNDER", raising=False)
    monkeypatch.delenv("POLYMARKET_FUNDER", raising=False)

    class FakeClient:
        def get_address(self) -> str:
            return "0xeoa"

    assert resolve_positions_wallet_address(FakeClient()) == "0xeoa"


def test_resolve_positions_wallet_address_returns_none_without_inputs(monkeypatch) -> None:
    from tyrex_pm.venue.polymarket.clob_env import resolve_positions_wallet_address

    monkeypatch.delenv("TYREX_FUNDER", raising=False)
    monkeypatch.delenv("POLYMARKET_FUNDER", raising=False)
    assert resolve_positions_wallet_address(None) is None
