from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "generate_clob_api_creds.py"
    spec = importlib.util.spec_from_file_location("generate_clob_api_creds_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generate_clob_api_creds_prints_only_env_lines(monkeypatch, capsys) -> None:
    mod = _load_script()
    monkeypatch.setattr(mod, "_load_dotenv", lambda: None)
    for key in (
        "POLYMARKET_PK",
        "POLYMARKET_SIGNATURE_TYPE",
        "POLYMARKET_FUNDER",
        "TYREX_CLOB_HOST",
        "TYREX_BUILDER_CODE",
        "TYREX_BUILDER_ADDRESS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("POLYMARKET_PK", "0xabc")
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "2")
    monkeypatch.setenv("POLYMARKET_FUNDER", "0xfunder")

    calls: dict[str, object] = {}

    class _Client:
        def __init__(self, host, **kwargs):
            calls["host"] = host
            calls["kwargs"] = kwargs

        def derive_api_key(self):
            return SimpleNamespace(
                api_key="key",
                api_secret="secret",
                api_passphrase="passphrase",
            )

        def create_api_key(self):
            raise AssertionError("derive-first should succeed for this fake")

    fake_v2 = SimpleNamespace(ClobClient=_Client)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", fake_v2)

    assert mod.main() == 0
    out = capsys.readouterr()
    assert out.err == ""
    assert out.out == (
        "POLYMARKET_API_KEY=key\n"
        "POLYMARKET_API_SECRET=secret\n"
        "POLYMARKET_PASSPHRASE=passphrase\n"
    )
    assert calls["host"] == "https://clob.polymarket.com"
    assert calls["kwargs"] == {
        "chain_id": 137,
        "key": "0xabc",
        "signature_type": 2,
        "funder": "0xfunder",
        "builder_config": None,
    }


def test_generate_clob_api_creds_requires_funder_for_safe(monkeypatch, capsys) -> None:
    mod = _load_script()
    monkeypatch.setattr(mod, "_load_dotenv", lambda: None)
    monkeypatch.setenv("POLYMARKET_PK", "0xabc")
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "2")
    monkeypatch.delenv("POLYMARKET_FUNDER", raising=False)
    monkeypatch.delenv("TYREX_FUNDER", raising=False)

    assert mod.main() == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "POLYMARKET_FUNDER" in out.err
    assert "required when signature_type is non-EOA" in out.err
