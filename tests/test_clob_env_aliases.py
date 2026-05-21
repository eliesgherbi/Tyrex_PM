from __future__ import annotations

import re
import sys
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Heartbeat-id helpers (unchanged in V2; kept here as the focused unit tests
# already covered them and they are the same code path).
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# V2 client construction boundary (Phase 1).
#
# These tests patch ``py_clob_client_v2.ClobClient`` (the real top-level export
# in the installed V2 SDK) and assert the env wiring, including the post-cutover
# production host default, env override, signature-type fallback, funder
# fallback, and Tyrex ``_derive_or_create_api_key`` (derive-first bootstrap).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_v2_env(monkeypatch):
    """Ensure each test starts with no V2-specific env contamination."""
    for var in (
        "TYREX_CLOB_HOST",
        "TYREX_CHAIN_ID",
        "TYREX_PRIVATE_KEY",
        "POLYMARKET_PK",
        "TYREX_SIGNATURE_TYPE",
        "POLYMARKET_SIGNATURE_TYPE",
        "TYREX_FUNDER",
        "POLYMARKET_FUNDER",
        "TYREX_BUILDER_CODE",
        "TYREX_BUILDER_ADDRESS",
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_PASSPHRASE",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _patched_v2_client():
    """Helper: build a MagicMock that mimics the V2 ClobClient API surface used here."""
    instance = MagicMock(name="V2ClobClient")
    creds = MagicMock(name="ApiCreds")
    creds.api_key = "derived-key"
    creds.api_secret = "sec"
    creds.api_passphrase = "pp"
    instance.derive_api_key.return_value = creds
    return instance


def test_returns_none_when_no_private_key(monkeypatch) -> None:
    from tyrex_pm.venue.polymarket import clob_env

    assert clob_env.try_create_clob_client() is None


def test_v2_client_constructed_with_default_post_cutover_host(monkeypatch) -> None:
    monkeypatch.setenv("TYREX_PRIVATE_KEY", "0xabc123")
    instance = _patched_v2_client()
    with patch("py_clob_client_v2.ClobClient", return_value=instance) as mock_cls:
        from tyrex_pm.venue.polymarket import clob_env

        result = clob_env.try_create_clob_client()
    assert result is instance
    mock_cls.assert_called_once()
    args, kwargs = mock_cls.call_args
    assert args[0] == "https://clob.polymarket.com"
    assert kwargs["chain_id"] == 137
    assert kwargs["key"] == "0xabc123"
    assert kwargs["signature_type"] == 0
    assert kwargs["funder"] is None
    assert kwargs["builder_config"] is None
    instance.derive_api_key.assert_called_once_with()
    instance.create_api_key.assert_not_called()
    instance.set_api_creds.assert_called_once()


def test_default_host_module_constant_is_post_cutover_production() -> None:
    from tyrex_pm.venue.polymarket import clob_env

    assert clob_env.DEFAULT_CLOB_HOST_V2 == "https://clob.polymarket.com"
    assert clob_env.PRE_CUTOVER_CLOB_HOST_V2 == "https://clob-v2.polymarket.com"


def test_env_override_wins_over_v2_default(monkeypatch) -> None:
    monkeypatch.setenv("TYREX_PRIVATE_KEY", "0xabc123")
    monkeypatch.setenv("TYREX_CLOB_HOST", "https://clob.polymarket.com")
    instance = _patched_v2_client()
    with patch("py_clob_client_v2.ClobClient", return_value=instance) as mock_cls:
        from tyrex_pm.venue.polymarket import clob_env

        clob_env.try_create_clob_client()
    args, _kwargs = mock_cls.call_args
    assert args[0] == "https://clob.polymarket.com"


def test_old_pre_cutover_host_override_is_rewritten_with_warning(monkeypatch, caplog) -> None:
    monkeypatch.setenv("TYREX_PRIVATE_KEY", "0xabc123")
    monkeypatch.setenv("TYREX_CLOB_HOST", "https://clob-v2.polymarket.com")
    instance = _patched_v2_client()
    with patch("py_clob_client_v2.ClobClient", return_value=instance) as mock_cls:
        from tyrex_pm.venue.polymarket import clob_env

        clob_env.try_create_clob_client()
    args, _kwargs = mock_cls.call_args
    assert args[0] == "https://clob.polymarket.com"
    assert "pre-cutover V2 transition host" in caplog.text


def test_polymarket_pk_fallback(monkeypatch) -> None:
    monkeypatch.setenv("POLYMARKET_PK", "0xabc123")
    instance = _patched_v2_client()
    with patch("py_clob_client_v2.ClobClient", return_value=instance) as mock_cls:
        from tyrex_pm.venue.polymarket import clob_env

        clob_env.try_create_clob_client()
    assert mock_cls.call_args.kwargs["key"] == "0xabc123"


def test_polymarket_signature_type_fallback(monkeypatch) -> None:
    monkeypatch.setenv("TYREX_PRIVATE_KEY", "0xabc123")
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "1")
    instance = _patched_v2_client()
    with patch("py_clob_client_v2.ClobClient", return_value=instance) as mock_cls:
        from tyrex_pm.venue.polymarket import clob_env

        clob_env.try_create_clob_client()
    assert mock_cls.call_args.kwargs["signature_type"] == 1


def test_polymarket_funder_fallback(monkeypatch) -> None:
    monkeypatch.setenv("TYREX_PRIVATE_KEY", "0xabc123")
    monkeypatch.setenv("POLYMARKET_FUNDER", "0xfunder")
    instance = _patched_v2_client()
    with patch("py_clob_client_v2.ClobClient", return_value=instance) as mock_cls:
        from tyrex_pm.venue.polymarket import clob_env

        clob_env.try_create_clob_client()
    assert mock_cls.call_args.kwargs["funder"] == "0xfunder"


def test_returns_none_when_v2_sdk_missing(monkeypatch) -> None:
    """If py-clob-client-v2 is not importable, function logs a warning and returns None."""
    monkeypatch.setenv("TYREX_PRIVATE_KEY", "0xabc123")

    real_import = __import__

    def fake_import(name, *a, **kw):
        if name.startswith("py_clob_client_v2"):
            raise ImportError("simulated missing V2 SDK")
        return real_import(name, *a, **kw)

    with patch("builtins.__import__", side_effect=fake_import):
        from tyrex_pm.venue.polymarket import clob_env

        assert clob_env.try_create_clob_client() is None


def test_returns_none_when_api_key_bootstrap_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("TYREX_PRIVATE_KEY", "0xabc123")
    instance = MagicMock()
    instance.derive_api_key.side_effect = Exception("nope")
    instance.create_api_key.side_effect = Exception("nope")
    with patch("py_clob_client_v2.ClobClient", return_value=instance):
        from tyrex_pm.venue.polymarket import clob_env

        assert clob_env.try_create_clob_client() is None


def test_precreated_clob_api_creds_from_env_skip_create_or_derive(monkeypatch) -> None:
    monkeypatch.setenv("TYREX_PRIVATE_KEY", "0xabc123")
    monkeypatch.setenv("POLYMARKET_API_KEY", "api-key")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "api-secret")
    monkeypatch.setenv("POLYMARKET_PASSPHRASE", "api-passphrase")
    instance = MagicMock()
    with patch("py_clob_client_v2.ClobClient", return_value=instance):
        from tyrex_pm.venue.polymarket import clob_env
        from py_clob_client_v2 import ApiCreds

        assert clob_env.try_create_clob_client() is instance
    instance.create_api_key.assert_not_called()
    instance.derive_api_key.assert_not_called()
    instance.set_api_creds.assert_called_once()
    creds = instance.set_api_creds.call_args.args[0]
    assert isinstance(creds, ApiCreds)
    assert creds.api_key == "api-key"
    assert creds.api_secret == "api-secret"
    assert creds.api_passphrase == "api-passphrase"


def test_partial_precreated_clob_api_creds_raise(monkeypatch) -> None:
    monkeypatch.setenv("TYREX_PRIVATE_KEY", "0xabc123")
    monkeypatch.setenv("POLYMARKET_API_KEY", "api-key")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "api-secret")
    from tyrex_pm.venue.polymarket import clob_env

    with pytest.raises(ValueError, match="partially configured"):
        clob_env.try_create_clob_client()


# ---------------------------------------------------------------------------
# Optional builder-code plumbing.
# ---------------------------------------------------------------------------


_VALID_BUILDER_CODE = "0x" + "ab" * 32
_VALID_BUILDER_ADDR = "0x" + "cd" * 20


def test_builder_config_built_when_code_and_address_set(monkeypatch) -> None:
    monkeypatch.setenv("TYREX_PRIVATE_KEY", "0xabc123")
    monkeypatch.setenv("TYREX_BUILDER_CODE", _VALID_BUILDER_CODE)
    monkeypatch.setenv("TYREX_BUILDER_ADDRESS", _VALID_BUILDER_ADDR)
    instance = _patched_v2_client()
    with patch("py_clob_client_v2.ClobClient", return_value=instance) as mock_cls:
        from tyrex_pm.venue.polymarket import clob_env

        clob_env.try_create_clob_client()
    bc = mock_cls.call_args.kwargs["builder_config"]
    from py_clob_client_v2 import BuilderConfig

    assert isinstance(bc, BuilderConfig)
    assert bc.builder_code == _VALID_BUILDER_CODE
    assert bc.builder_address == _VALID_BUILDER_ADDR


def test_builder_code_no_config_when_unset(monkeypatch) -> None:
    monkeypatch.setenv("TYREX_PRIVATE_KEY", "0xabc123")
    instance = _patched_v2_client()
    with patch("py_clob_client_v2.ClobClient", return_value=instance) as mock_cls:
        from tyrex_pm.venue.polymarket import clob_env

        clob_env.try_create_clob_client()
    assert mock_cls.call_args.kwargs["builder_config"] is None


def test_builder_code_malformed_raises(monkeypatch) -> None:
    monkeypatch.setenv("TYREX_PRIVATE_KEY", "0xabc123")
    monkeypatch.setenv("TYREX_BUILDER_CODE", "0xdeadbeef")  # too short
    monkeypatch.setenv("TYREX_BUILDER_ADDRESS", _VALID_BUILDER_ADDR)
    from tyrex_pm.venue.polymarket import clob_env

    with pytest.raises(ValueError, match="TYREX_BUILDER_CODE"):
        clob_env.try_create_clob_client()


def test_builder_code_without_address_raises(monkeypatch) -> None:
    monkeypatch.setenv("TYREX_PRIVATE_KEY", "0xabc123")
    monkeypatch.setenv("TYREX_BUILDER_CODE", _VALID_BUILDER_CODE)
    from tyrex_pm.venue.polymarket import clob_env

    with pytest.raises(ValueError, match="TYREX_BUILDER_ADDRESS"):
        clob_env.try_create_clob_client()


def test_builder_address_malformed_raises(monkeypatch) -> None:
    monkeypatch.setenv("TYREX_PRIVATE_KEY", "0xabc123")
    monkeypatch.setenv("TYREX_BUILDER_CODE", _VALID_BUILDER_CODE)
    monkeypatch.setenv("TYREX_BUILDER_ADDRESS", "not-an-address")
    from tyrex_pm.venue.polymarket import clob_env

    with pytest.raises(ValueError, match="TYREX_BUILDER_ADDRESS"):
        clob_env.try_create_clob_client()


# ---------------------------------------------------------------------------
# Source-of-truth assertions: the V2 SDK actually exposes the symbols / methods
# Phase 1 depends on. If any of these break, V2 SDK surface drift is the cause.
# ---------------------------------------------------------------------------


def test_v2_sdk_exposes_required_phase1_surface() -> None:
    """Phase 0 surface check: pin the V2 symbols Phase 1 actually uses."""
    import py_clob_client_v2 as v2

    for sym in ("ClobClient", "ApiCreds", "BuilderConfig", "SignatureTypeV2"):
        assert hasattr(v2, sym), f"py-clob-client-v2 missing required symbol: {sym}"

    client_cls = v2.ClobClient
    for method in (
        "create_or_derive_api_key",
        "derive_api_key",
        "create_api_key",
        "set_api_creds",
        "post_heartbeat",
        "get_address",
    ):
        assert hasattr(client_cls, method), f"V2 ClobClient missing method: {method}"

    from py_clob_client_v2.exceptions import PolyApiException

    assert issubclass(PolyApiException, Exception)
