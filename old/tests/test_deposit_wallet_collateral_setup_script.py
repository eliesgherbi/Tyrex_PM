from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "deposit_wallet_collateral_setup.py"
    spec = importlib.util.spec_from_file_location("deposit_wallet_collateral_setup_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_encode_approve_prefix_and_calldata_length() -> None:
    mod = _load_script()
    spender = "0xE111180000d2663C0091e4f400237545B87B996B"
    hx = mod._encode_approve(spender)
    assert hx.startswith("0x095ea7b3")
    assert len(hx) == 2 + 8 + 64 + 64  # selector + 2 words


def test_parse_balance_allowance_v2_shape() -> None:
    mod = _load_script()
    bal = {
        "balance": "30625001",
        "allowances": {"0xaa": "1000000", "0xbb": "2000000"},
    }
    b, a = mod._parse_balance_allowance(bal)
    assert b == "30.625001"
    assert a == "1"


def test_approve_calls_four_spenders_polygon_mainnet() -> None:
    pytest.importorskip("py_clob_client_v2")
    mod = _load_script()
    calls = mod._approve_calls_for_polymarket_v2(137)
    assert len(calls) == 4
    pusd = calls[0]["target"]
    assert all(c["target"] == pusd for c in calls)
    assert all(c["value"] == "0" for c in calls)
    assert all(c["data"].startswith("0x095ea7b3") for c in calls)
