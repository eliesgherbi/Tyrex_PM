from __future__ import annotations

import re
from unittest.mock import MagicMock, patch


def test_resolve_clob_heartbeat_id_random_hex(monkeypatch) -> None:
    monkeypatch.delenv("TYREX_HEARTBEAT_ID", raising=False)
    from tyrex_pm.venue.polymarket import clob_env

    hid = clob_env.resolve_clob_heartbeat_id()
    assert re.fullmatch(r"[0-9a-f]{32}", hid)


def test_resolve_clob_heartbeat_id_strips_uuid_hyphens(monkeypatch) -> None:
    monkeypatch.setenv("TYREX_HEARTBEAT_ID", "115B0866-F68E-4ED9-BB44-516DD5B3EF00")
    from tyrex_pm.venue.polymarket import clob_env

    assert clob_env.resolve_clob_heartbeat_id() == "115b0866f68e4ed9bb44516dd5b3ef00"


def test_normalize_heartbeat_id_for_clob_strips_uuid() -> None:
    from tyrex_pm.venue.polymarket import clob_env

    assert (
        clob_env.normalize_heartbeat_id_for_clob("cad758a9-b067-48e1-b0b7-383a576d9252")
        == "cad758a9b06748e1b0b7383a576d9252"
    )


def test_normalize_heartbeat_id_empty_and_none() -> None:
    from tyrex_pm.venue.polymarket import clob_env

    assert clob_env.normalize_heartbeat_id_for_clob(None) == ""
    assert clob_env.normalize_heartbeat_id_for_clob("") == ""


def test_polymarket_heartbeat_id_env_fallback(monkeypatch) -> None:
    monkeypatch.delenv("TYREX_HEARTBEAT_ID", raising=False)
    monkeypatch.setenv("POLYMARKET_HEARTBEAT_ID", "44174643-7805-408a-a210-45d2ff99c8a9")
    from tyrex_pm.venue.polymarket import clob_env

    assert clob_env.resolve_clob_heartbeat_id() == "441746437805408aa21045d2ff99c8a9"


def test_polymarket_pk_fallback(monkeypatch) -> None:
    monkeypatch.delenv("TYREX_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("POLYMARKET_PK", "0xabc123")
    instance = MagicMock()
    instance.create_or_derive_api_creds.return_value = MagicMock()
    with patch("py_clob_client.client.ClobClient", return_value=instance) as mock_cls:
        from tyrex_pm.venue.polymarket import clob_env

        clob_env.try_create_clob_client()
    mock_cls.assert_called_once()
    assert mock_cls.call_args.kwargs["key"] == "0xabc123"


def test_polymarket_signature_type_fallback(monkeypatch) -> None:
    monkeypatch.setenv("TYREX_PRIVATE_KEY", "0xabc123")
    monkeypatch.delenv("TYREX_SIGNATURE_TYPE", raising=False)
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "1")
    instance = MagicMock()
    instance.create_or_derive_api_creds.return_value = MagicMock()
    with patch("py_clob_client.client.ClobClient", return_value=instance) as mock_cls:
        from tyrex_pm.venue.polymarket import clob_env

        clob_env.try_create_clob_client()
    assert mock_cls.call_args.kwargs["signature_type"] == 1


def test_polymarket_funder_fallback(monkeypatch) -> None:
    monkeypatch.setenv("TYREX_PRIVATE_KEY", "0xabc123")
    monkeypatch.delenv("TYREX_FUNDER", raising=False)
    monkeypatch.setenv("POLYMARKET_FUNDER", "0xfunder")
    instance = MagicMock()
    instance.create_or_derive_api_creds.return_value = MagicMock()
    with patch("py_clob_client.client.ClobClient", return_value=instance) as mock_cls:
        from tyrex_pm.venue.polymarket import clob_env

        clob_env.try_create_clob_client()
    assert mock_cls.call_args.kwargs["funder"] == "0xfunder"
