#!/usr/bin/env python3
"""Safe diagnostic + setup for Polymarket V2 deposit-wallet trading.

Follows: https://docs.polymarket.com/trading/deposit-wallets

**Safety:** default behavior is **dry-run**. Relayer transactions (`WALLET-CREATE`,
``WALLET`` batches) run **only** with ``--execute``. Private keys are never printed.

Required env (aliases supported):

* **Signer:** ``PRIVATE_KEY`` | ``TYREX_PRIVATE_KEY`` | ``POLYMARKET_PK``
* **Polygon reads:** ``RPC_URL`` (HTTPS JSON-RPC). Optional for identity + ``--sync-clob`` only;
  **required** for ``--status`` on-chain section and for ``--wrap-*`` (balance read).
* **Relayer:** ``RELAYER_URL``, ``BUILDER_API_KEY``, ``BUILDER_SECRET``,
  ``BUILDER_PASS_PHRASE`` (or ``POLYMARKET_BUILDER_*`` / ``BUILDER_PASSPHRASE``)
* **CLOB (for sync):** ``CLOB_API_URL`` | ``TYREX_CLOB_HOST`` | default prod host;
  ``CLOB_API_KEY`` / ``CLOB_SECRET`` / ``CLOB_PASS_PHRASE`` or
  ``POLYMARKET_API_KEY`` / ``POLYMARKET_API_SECRET`` / ``POLYMARKET_PASSPHRASE``

Dependencies::

    pip install -U py-clob-client-v2 "py-builder-relayer-client>=0.0.2rc1" \\
        py-builder-signing-sdk eth_abi eth_account eth_utils httpx python-dotenv

``py-clob-client-v2`` pins HTTPX 0.27+/``http2``; if you see ``unexpected keyword 'http2'``,
downgrade: ``pip install "httpx>=0.27,<1"`` (avoid HTTPX 1.0 pre-releases in the same env).

Examples::

    # Read-only status (on-chain + optional CLOB)
    python scripts/setup_deposit_wallet_v2.py --status --sync-clob
    python scripts/setup_deposit_wallet_v2.py --debug-clob-balance-allowance

    # Mutations (still prints plan first)
    python scripts/setup_deposit_wallet_v2.py --deploy --execute
    python scripts/setup_deposit_wallet_v2.py --wrap-all-usdce --execute
    python scripts/setup_deposit_wallet_v2.py --approve-all --execute
    python scripts/setup_deposit_wallet_v2.py --approve-collateral-adapters --execute  # pUSD → adapters only

    # Full dry-run plan
    python scripts/setup_deposit_wallet_v2.py --status --wrap-all-usdce --approve-all --sync-clob
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from eth_abi import encode as abi_encode
from eth_utils.crypto import keccak as keccak_hash
from eth_utils import to_checksum_address

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# --- Polygon mainnet constants (Polymarket docs / contracts page) ---
CHAIN_ID = 137
EXPECTED_DEPOSIT_WALLET = "0x4Fac05D2bcA7C2B6c74d948DD4248D3dB39A7A19"
DEPOSIT_WALLET_FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# Native USDC on Polygon (informational balance line only)
POLYGON_NATIVE_USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
COLLATERAL_ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"
CTF_EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_CTF_EXCHANGE = "0xe2222d279d744050d28e00520010520000310F59"
CTF_COLLATERAL_ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"
NEG_RISK_CTF_COLLATERAL_ADAPTER = "0xadA2005600Dec949baf300f4C6120000bDB6eAab"
# CLOB collateral mirror lists this spender alongside exchanges (Neg Risk path)
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

MAX_UINT256 = 2**256 - 1
DEFAULT_RELAYER_URL = "https://relayer-v2.polymarket.com"
DEFAULT_CLOB_HOST = "https://clob.polymarket.com"
DEADLINE_OFFSET_S = 600

# selectors
SEL_APPROVE = bytes.fromhex("095ea7b3")
SEL_BALANCE_OF = bytes.fromhex("70a08231")
SEL_ALLOWANCE = bytes.fromhex("dd62ed3e")
SEL_WRAP = keccak_hash(primitive=b"wrap(address,address,uint256)")[:4]
SEL_SET_APPROVAL_FOR_ALL = bytes.fromhex("a22cb465")
SEL_IS_APPROVED_FOR_ALL = bytes.fromhex("e985e9c5")
# ERC20 transfer(address,uint256)
SEL_ERC20_TRANSFER = bytes.fromhex("a9059cbb")

BRIDGE_DEPOSIT_API_URL = "https://bridge.polymarket.com/deposit"

DEPLOY_RELAYER_MISMATCH_WARN = (
    "Wallet has bytecode on-chain but relayer get_deployed() returned false. "
    "Do not redeploy automatically. WALLET batches may still work; if they fail, "
    "inspect relayer/factory/owner configuration."
)

_HTTPX_CLOB_COMPAT_PATCHED = False


def _ensure_httpx_clob_compat() -> None:
    """``py-clob-client-v2`` instantiates ``httpx.Client(http2=True)``. HTTPX 1.0 pre-releases
    removed the ``http2`` keyword; patch ``httpx.Client`` so unknown kwargs are dropped before
    the SDK is imported.
    """
    global _HTTPX_CLOB_COMPAT_PATCHED
    if _HTTPX_CLOB_COMPAT_PATCHED:
        return
    import inspect

    import httpx

    try:
        sig = inspect.signature(httpx.Client.__init__)
        if "http2" in sig.parameters:
            _HTTPX_CLOB_COMPAT_PATCHED = True
            return
    except (TypeError, ValueError):
        pass

    _orig = httpx.Client

    def _client_strip_http2(*args, **kwargs):
        kwargs.pop("http2", None)
        return _orig(*args, **kwargs)

    httpx.Client = _client_strip_http2  # type: ignore[method-assign, assignment]
    _HTTPX_CLOB_COMPAT_PATCHED = True


def _load_dotenv() -> None:
    p = REPO_ROOT / ".env"
    if not p.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(p, override=True)
    except ImportError:
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'")


def _env_first(*names: str) -> str:
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""


def _pk() -> str:
    k = _env_first("PRIVATE_KEY", "TYREX_PRIVATE_KEY", "POLYMARKET_PK")
    return k


def _rpc_url() -> str:
    return _env_first("RPC_URL").rstrip("/")


def _encode_approve(spender: str, amount: int = MAX_UINT256) -> str:
    body = abi_encode(["address", "uint256"], [to_checksum_address(spender), amount])
    return "0x" + (SEL_APPROVE + body).hex()


def _encode_wrap(asset: str, to: str, amount: int) -> str:
    body = abi_encode(
        ["address", "address", "uint256"],
        [to_checksum_address(asset), to_checksum_address(to), amount],
    )
    return "0x" + (SEL_WRAP + body).hex()


def _encode_set_approval_for_all(operator: str, approved: bool = True) -> str:
    body = abi_encode(["address", "bool"], [to_checksum_address(operator), approved])
    return "0x" + (SEL_SET_APPROVAL_FOR_ALL + body).hex()


def _encode_balance_of(holder: str) -> str:
    body = abi_encode(["address"], [to_checksum_address(holder)])
    return "0x" + (SEL_BALANCE_OF + body).hex()


def _encode_allowance(owner: str, spender: str) -> str:
    body = abi_encode(
        ["address", "address"],
        [to_checksum_address(owner), to_checksum_address(spender)],
    )
    return "0x" + (SEL_ALLOWANCE + body).hex()


def _encode_is_approved_for_all(account: str, operator: str) -> str:
    body = abi_encode(
        ["address", "address"],
        [to_checksum_address(account), to_checksum_address(operator)],
    )
    return "0x" + (SEL_IS_APPROVED_FOR_ALL + body).hex()


def _encode_erc20_transfer(to_addr: str, amount: int) -> str:
    body = abi_encode(["address", "uint256"], [to_checksum_address(to_addr), amount])
    return "0x" + (SEL_ERC20_TRANSFER + body).hex()


def _rpc_call(url: str, method: str, params: list[Any]) -> dict[str, Any]:
    r = httpx.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=60.0,
    )
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"RPC error: {body['error']}")
    return body


def _eth_call(url: str, to: str, data: str) -> bytes:
    res = _rpc_call(
        url,
        "eth_call",
        [{"to": to_checksum_address(to), "data": data}, "latest"],
    )
    out = res.get("result") or "0x"
    if not isinstance(out, str) or out == "0x":
        return b""
    return bytes.fromhex(out[2:])


def _eth_get_code(url: str, addr: str) -> bytes:
    res = _rpc_call(url, "eth_getCode", [to_checksum_address(addr), "latest"])
    hx = res.get("result") or "0x"
    if not isinstance(hx, str) or hx == "0x":
        return b""
    return bytes.fromhex(hx[2:])


def _decode_uint256(raw: bytes) -> int:
    if not raw:
        return 0
    return int.from_bytes(raw[-32:], "big")


def _fmt_usd6(raw_amt: int) -> str:
    return str(Decimal(raw_amt) / Decimal(10**6))


def _ensure_relayer_sdk():
    try:
        from py_builder_relayer_client.client import RelayClient  # noqa: F401
        from py_builder_relayer_client.models import DepositWalletCall  # noqa: F401
    except ImportError as e:
        raise SystemExit(
            "Install py-builder-relayer-client>=0.0.2rc1 (deposit-wallet support):\n"
            '  pip install -U --pre "py-builder-relayer-client>=0.0.2rc1"\n'
            f"Import error: {e}"
        ) from e


def _derived_deposit_wallet_from_pk(pk: str) -> str:
    """Deterministic deposit wallet for owner (no relayer HTTP / builder credentials)."""
    _ensure_relayer_sdk()
    from eth_account import Account

    from py_builder_relayer_client.builder.derive import derive_deposit_wallet
    from py_builder_relayer_client.config import get_contract_config

    owner = to_checksum_address(Account.from_key(pk).address)
    cfg = get_contract_config(CHAIN_ID)
    return to_checksum_address(
        derive_deposit_wallet(
            owner,
            cfg.deposit_wallet_factory,
            cfg.deposit_wallet_implementation,
        )
    )


def _builder_config():
    from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig

    key = _env_first("BUILDER_API_KEY", "POLYMARKET_BUILDER_API_KEY")
    secret = _env_first("BUILDER_SECRET", "POLYMARKET_BUILDER_SECRET")
    passphrase = _env_first(
        "BUILDER_PASS_PHRASE",
        "BUILDER_PASSPHRASE",
        "POLYMARKET_BUILDER_PASSPHRASE",
    )
    if not (key and secret and passphrase):
        raise SystemExit(
            "Missing Builder API credentials. Set BUILDER_API_KEY, BUILDER_SECRET, "
            "BUILDER_PASS_PHRASE (or POLYMARKET_BUILDER_* equivalents)."
        )
    return BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=key, secret=secret, passphrase=passphrase
        )
    )


def _make_clob_client(deposit_wallet: str) -> tuple[Any, str]:
    """Build ``ClobClient`` exactly like ``--sync-clob``. Returns (client, creds_source_label)."""
    _ensure_httpx_clob_compat()
    try:
        from py_clob_client_v2 import ApiCreds, ClobClient, SignatureTypeV2
    except ImportError as e:
        raise SystemExit(f"Install py-clob-client-v2: pip install py-clob-client-v2\n{e}") from e

    pk = _pk()
    host = _env_first("CLOB_API_URL", "TYREX_CLOB_HOST", "POLYMARKET_CLOB_HOST") or DEFAULT_CLOB_HOST
    key = _env_first("CLOB_API_KEY", "POLYMARKET_API_KEY")
    secret = _env_first("CLOB_SECRET", "POLYMARKET_API_SECRET")
    phrase = _env_first("CLOB_PASS_PHRASE", "POLYMARKET_PASSPHRASE")
    client = ClobClient(
        host,
        chain_id=CHAIN_ID,
        key=pk,
        signature_type=SignatureTypeV2.POLY_1271,
        funder=to_checksum_address(deposit_wallet),
    )
    if key and secret and phrase:
        client.set_api_creds(ApiCreds(api_key=key, api_secret=secret, api_passphrase=phrase))
        return client, "env (CLOB_API_KEY / POLYMARKET_API_KEY + secret + passphrase)"
    from tyrex_pm.venue.polymarket.clob_env import _derive_or_create_api_key

    creds = _derive_or_create_api_key(client)
    if creds is None:
        raise SystemExit(
            "Could not create/derive CLOB API credentials. Set CLOB_API_KEY, "
            "CLOB_SECRET, CLOB_PASS_PHRASE (or POLYMARKET_API_*)."
        )
    client.set_api_creds(creds)
    return client, "_derive_or_create_api_key()"


def _clob_client(deposit_wallet: str):
    client, _ = _make_clob_client(deposit_wallet)
    return client


def _redact_poly_api_header_value(name: str, value: str) -> str:
    if name == "POLY_PASSPHRASE":
        return "(not printed)"
    if name in ("POLY_SIGNATURE", "POLY_API_KEY"):
        v = str(value)
        if len(v) <= 8:
            return "***"
        return f"{v[:4]}…{v[-4:]}"
    return value


def _redact_l2_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: _redact_poly_api_header_value(k, str(v)) for k, v in headers.items()}


def _api_key_prefix_only(client: Any) -> str:
    c = getattr(client, "creds", None)
    if not c:
        return "(none)"
    k = getattr(c, "api_key", "") or ""
    if len(k) <= 8:
        return f"{k[:2]}…" if k else "(empty)"
    return f"{k[:6]}…{k[-4:]}"


def cmd_debug_clob_balance_allowance(deposit_wallet: str, owner_eoa: str) -> None:
    """SDK + raw GET ``/balance-allowance/update`` and ``/balance-allowance`` for COLLATERAL."""
    _ensure_httpx_clob_compat()
    from py_clob_client_v2 import AssetType, BalanceAllowanceParams, SignatureTypeV2
    from py_clob_client_v2.clob_types import RequestArgs
    from py_clob_client_v2.endpoints import GET_BALANCE_ALLOWANCE, UPDATE_BALANCE_ALLOWANCE
    from py_clob_client_v2.headers.headers import create_level_2_headers

    client, creds_source = _make_clob_client(deposit_wallet)
    dw = to_checksum_address(deposit_wallet)
    sig_int = int(client.builder.signature_type)
    host_eff = client.host.rstrip("/")
    params_q = {"asset_type": AssetType.COLLATERAL, "signature_type": sig_int}

    print("--- Redacted CLOB client identity ---")
    print(f"  owner_eoa (signer / POLY_ADDRESS): {owner_eoa}")
    print(f"  funder (deposit wallet)          : {dw}")
    print(f"  signature_type                   : {sig_int} (POLY_1271={int(SignatureTypeV2.POLY_1271)})")
    print(f"  CLOB host                        : {host_eff}")
    print(f"  API key (prefix / suffix only)   : {_api_key_prefix_only(client)}")
    print(f"  API creds source                 : {creds_source}")
    print(f"  BalanceAllowanceParams fields    : asset_type, token_id, signature_type (default -1)")
    print(
        "  SDK note: ``get_balance_allowance`` / ``update_balance_allowance`` put "
        "``signature_type`` from OrderBuilder (constructor), not from BalanceAllowanceParams."
    )
    print(
        "  Params note: there is no ``funder`` query field on these endpoints in the SDK; "
        "deposit-wallet flow relies on constructor ``funder=`` + signer key + POLY_1271."
    )
    print()

    bal_params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)

    print("--- Via SDK (py-clob-client-v2) ---")
    upd_sdk = client.update_balance_allowance(bal_params)
    print("update_balance_allowance() response:")
    if isinstance(upd_sdk, dict):
        print(json.dumps(upd_sdk, indent=2))
    elif upd_sdk == "":
        print(json.dumps({"body": "(empty string)", "_note": "HTTP 200 with empty JSON/text body"}, indent=2))
    else:
        print(json.dumps({"raw": upd_sdk}, indent=2, default=str))
    print()
    get_sdk = client.get_balance_allowance(bal_params)
    print("get_balance_allowance() response:")
    if isinstance(get_sdk, dict):
        print(json.dumps(get_sdk, indent=2))
        ad = get_sdk.get("allowances")
        if isinstance(ad, dict) and ad:
            zero_spenders = [
                sp for sp, raw in ad.items() if raw in (0, "0", "0x0") or str(raw) == "0"
            ]
            if zero_spenders:
                print()
                print(
                    "  NOTE: at least one spender in ``allowances`` is zero "
                    f"(min allowance → 0): {zero_spenders}"
                )
    else:
        print(json.dumps({"raw": get_sdk}, indent=2, default=str))
    print()

    print("--- Raw HTTP (same L2 signing as SDK ``create_level_2_headers``) ---")

    def _do_raw(label: str, path: str) -> None:
        ra = RequestArgs(method="GET", request_path=path)
        headers = create_level_2_headers(client.signer, client.creds, ra, timestamp=None)
        url = f"{host_eff}{path}"
        redacted = _redact_l2_headers(headers)
        print(f"{label}")
        print(f"  GET {path}")
        print(f"  query_params: {json.dumps(params_q)}")
        print(f"  L2 headers (redacted): {json.dumps(redacted, indent=4)}")
        r = httpx.get(url, headers=headers, params=params_q, timeout=60.0)
        print(f"  HTTP status: {r.status_code}")
        try:
            body = r.json()
            printable = body if isinstance(body, dict) else {"raw": body}
        except Exception:
            printable = {"text": r.text[:8000]}
        print(f"  response JSON:")
        print(json.dumps(printable, indent=2))
        print()

    _do_raw("1) Update", UPDATE_BALANCE_ALLOWANCE)
    _do_raw("2) Read", GET_BALANCE_ALLOWANCE)


def _parse_balance_allowance(bal: dict[str, Any]) -> tuple[str, str]:
    from decimal import Decimal

    allowances_dict = bal.get("allowances")
    is_v2 = isinstance(allowances_dict, dict) and bool(allowances_dict)
    raw_bal = bal.get("balance") or bal.get("available")
    scale = Decimal(10) ** 6

    def dec(x: Any) -> Decimal:
        try:
            return Decimal(str(x))
        except Exception:
            return Decimal(0)

    b_out = str(dec(raw_bal) / scale) if raw_bal is not None and is_v2 else (
        str(dec(raw_bal)) if raw_bal is not None else "?"
    )
    if is_v2:
        try:
            a_out = str(min(dec(v) for v in allowances_dict.values()) / scale)
        except ValueError:
            a_out = "?"
    else:
        legacy = bal.get("allowance") or bal.get("allowance_balance")
        a_out = str(dec(legacy)) if legacy is not None else "?"
    return b_out, a_out


def _print_call(title: str, target: str, data_hex: str, purpose: str, extra: str = "") -> None:
    print(f"  [{title}]")
    print(f"    target : {to_checksum_address(target)}")
    print(f"    purpose: {purpose}")
    if extra:
        print(f"    detail : {extra}")
    print(f"    data   : {data_hex[:66]}{'…' if len(data_hex) > 66 else ''} ({len(data_hex)//2-1} bytes payload)")


@dataclass
class Report:
    owner_eoa: str = ""
    deposit_wallet: str = ""
    deployed: bool = False
    pusd_balance: str = "0"
    usdce_balance: str = "0"
    native_usdc_balance: str = "0"
    native_usdc_raw: int = 0
    pusd_allow_ctf: str = "0"
    pusd_allow_neg: str = "0"
    pusd_allow_ctf_collateral_adapter: str = "0"
    pusd_allow_neg_risk_ctf_collateral_adapter: str = "0"
    pusd_allow_neg_risk_adapter: str = "0"
    ctf_approved: bool = False
    neg_ctf_approved: bool = False
    clob_balance: str = "?"
    clob_allowance: str = "?"
    warnings: list[str] = field(default_factory=list)


def cmd_status(rpc_url: str, deposit_wallet: str, report: Report) -> None:
    dw = to_checksum_address(deposit_wallet)
    code = _eth_get_code(rpc_url, dw)
    report.deployed = len(code) > 0
    report.deposit_wallet = dw

    # ERC20 balances (USDC.e / pUSD / native USDC)
    for token, label, attr in (
        (PUSD, "pUSD", "pusd_balance"),
        (USDC_E, "USDC.e", "usdce_balance"),
        (POLYGON_NATIVE_USDC, "Polygon native USDC", "native_usdc_balance"),
    ):
        raw = _decode_uint256(_eth_call(rpc_url, token, _encode_balance_of(dw)))
        if token.lower() == POLYGON_NATIVE_USDC.lower():
            report.native_usdc_raw = raw
        setattr(report, attr, _fmt_usd6(raw))

    report.pusd_allow_ctf = _fmt_usd6(
        _decode_uint256(_eth_call(rpc_url, PUSD, _encode_allowance(dw, CTF_EXCHANGE)))
    )
    report.pusd_allow_neg = _fmt_usd6(
        _decode_uint256(_eth_call(rpc_url, PUSD, _encode_allowance(dw, NEG_RISK_CTF_EXCHANGE)))
    )
    report.pusd_allow_ctf_collateral_adapter = _fmt_usd6(
        _decode_uint256(_eth_call(rpc_url, PUSD, _encode_allowance(dw, CTF_COLLATERAL_ADAPTER)))
    )
    report.pusd_allow_neg_risk_ctf_collateral_adapter = _fmt_usd6(
        _decode_uint256(
            _eth_call(rpc_url, PUSD, _encode_allowance(dw, NEG_RISK_CTF_COLLATERAL_ADAPTER))
        )
    )
    report.pusd_allow_neg_risk_adapter = _fmt_usd6(
        _decode_uint256(_eth_call(rpc_url, PUSD, _encode_allowance(dw, NEG_RISK_ADAPTER)))
    )

    o_ctf = _decode_uint256(
        _eth_call(rpc_url, CONDITIONAL_TOKENS, _encode_is_approved_for_all(dw, CTF_EXCHANGE))
    )
    o_neg = _decode_uint256(
        _eth_call(rpc_url, CONDITIONAL_TOKENS, _encode_is_approved_for_all(dw, NEG_RISK_CTF_EXCHANGE))
    )
    report.ctf_approved = o_ctf != 0
    report.neg_ctf_approved = o_neg != 0


def cmd_sync_clob(deposit_wallet: str, report: Report, token_id: str | None) -> None:
    _ensure_httpx_clob_compat()
    from py_clob_client_v2 import AssetType, BalanceAllowanceParams

    client = _clob_client(deposit_wallet)
    if token_id:
        p = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
    else:
        p = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    client.update_balance_allowance(p)
    raw = client.get_balance_allowance(p)
    if isinstance(raw, dict):
        b, a = _parse_balance_allowance(raw)
        if token_id:
            print(f"  CLOB CONDITIONAL token_id={token_id} balance={b} allowance(min)={a}")
        else:
            report.clob_balance = b
            report.clob_allowance = a
            print(f"  CLOB COLLATERAL (signature_type=3 via client builder): balance={b} allowance(min)={a}")
    else:
        report.warnings.append(f"Unexpected CLOB balance response: {raw}")


def _extract_bridge_evm_address(payload: Any) -> str | None:
    """Best-effort parse of bridge.deposit JSON — schema may vary."""
    if isinstance(payload, dict):
        for key in ("address", "depositAddress", "deposit_address", "evmAddress", "to"):
            v = payload.get(key)
            if isinstance(v, str) and v.startswith("0x") and len(v) >= 42:
                try:
                    return to_checksum_address(v)
                except Exception:
                    return v
        nested = payload.get("data") or payload.get("result")
        if isinstance(nested, dict):
            return _extract_bridge_evm_address(nested)
    return None


def cmd_get_bridge_deposit_address(deposit_wallet: str) -> None:
    """POST bridge.polymarket.com/deposit — returns where to send native USDC for bridging."""
    dw = to_checksum_address(deposit_wallet)
    print("--- Bridge API: deposit routing address ---")
    print(f"  POST {BRIDGE_DEPOSIT_API_URL}")
    print(f'  JSON body: {{"address": "{dw}"}}')
    r = httpx.post(BRIDGE_DEPOSIT_API_URL, json={"address": dw}, timeout=60.0)
    r.raise_for_status()
    try:
        payload = r.json()
    except Exception:
        print(f"  Non-JSON response (truncated): {r.text[:2000]}")
        return
    print("  Response JSON:")
    print(json.dumps(payload, indent=2))
    print()
    evm = _extract_bridge_evm_address(payload)
    if evm:
        print(f"  Bridge EVM deposit address (parsed): {evm}")
        print()
    print(
        "  IMPORTANT: Send native Polygon USDC from MetaMask to the **bridge deposit EVM address** "
        "above, **not** directly to the deposit wallet. "
        "The bridge flow converts/wraps toward Polymarket collateral (pUSD per Polymarket routing)."
    )


def _print_next_step_commands(*, report: Report, rpc_url: str, native_raw_balance: int) -> None:
    """Suggest concrete follow-up commands from post-run snapshot."""
    pusd_r = Decimal(report.pusd_balance or "0")
    usdce_r = Decimal(report.usdce_balance or "0")
    native_pos = native_raw_balance > 0
    low_allow = (
        Decimal(report.pusd_allow_ctf or "0") <= 0 or Decimal(report.pusd_allow_neg or "0") <= 0
    )

    blocks: list[tuple[str, list[str]]] = []

    if rpc_url and native_pos and pusd_r <= 0:
        blocks.append(
            (
                "Native Polygon USDC on deposit wallet but pUSD == 0 (recovery)",
                [
                    "# Move stuck native USDC back to owner EOA (then bridge from MetaMask):",
                    "python scripts/setup_deposit_wallet_v2.py --transfer-native-usdc-to-owner --execute",
                    "",
                    "# Get the correct bridge deposit address for your deposit wallet:",
                    "python scripts/setup_deposit_wallet_v2.py --get-bridge-deposit-address",
                    "",
                    "# Send native USDC from MetaMask to the bridge EVM address printed above (not to deposit wallet).",
                    "",
                    "# After Polymarket credits pUSD / CLOB sees collateral:",
                    "python scripts/setup_deposit_wallet_v2.py --status --sync-clob",
                    "python scripts/setup_deposit_wallet_v2.py --approve-all --execute",
                    "python scripts/setup_deposit_wallet_v2.py --status --sync-clob",
                ],
            )
        )

    low_adapter = (
        Decimal(report.pusd_allow_ctf_collateral_adapter or "0") <= 0
        or Decimal(report.pusd_allow_neg_risk_ctf_collateral_adapter or "0") <= 0
        or Decimal(report.pusd_allow_neg_risk_adapter or "0") <= 0
    )
    low_allow = low_allow or low_adapter

    if rpc_url and pusd_r > 0 and low_allow:
        blocks.append(
            (
                "pUSD > 0 but allowances missing (exchanges, adapters, and/or Neg Risk Adapter)",
                [
                    "python scripts/setup_deposit_wallet_v2.py --approve-all --execute",
                    "python scripts/setup_deposit_wallet_v2.py --status --sync-clob",
                ],
            )
        )

    if rpc_url and usdce_r > 0:
        blocks.append(
            (
                "USDC.e on deposit wallet (CollateralOnramp wrap path)",
                [
                    "python scripts/setup_deposit_wallet_v2.py --wrap-all-usdce --execute",
                    "python scripts/setup_deposit_wallet_v2.py --sync-clob",
                ],
            )
        )

    if not blocks:
        return
    print()
    print("=" * 72)
    print("NEXT STEPS (suggested commands)")
    print("=" * 72)
    for title, lines in blocks:
        print(f"\n{title}:")
        for ln in lines:
            print(ln)


def _wallet_nonce(relayer) -> str:
    from py_builder_relayer_client.models import TransactionType

    owner = relayer.signer.address()
    payload = relayer.get_nonce(owner, TransactionType.WALLET.value)
    if payload is None or payload.get("nonce") is None:
        raise RuntimeError(f"Invalid WALLET nonce payload: {payload}")
    return str(payload["nonce"])


def _deadline() -> str:
    return str(int(time.time()) + DEADLINE_OFFSET_S)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Polymarket V2 deposit-wallet diagnostic/setup (dry-run unless --execute)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Submit relayer WALLET-CREATE / WALLET transactions (mutating)",
    )
    parser.add_argument("--status", action="store_true", help="On-chain + identity summary")
    parser.add_argument("--deploy", action="store_true", help="Plan or execute WALLET-CREATE")
    parser.add_argument("--wrap-all-usdce", action="store_true", help="Wrap full USDC.e balance via WALLET batch")
    parser.add_argument(
        "--wrap-amount",
        type=str,
        default=None,
        help="Wrap fixed raw amount (smallest USDC.e units, integer string), e.g. 5000000 = 5 USDC.e",
    )
    parser.add_argument(
        "--approve-buy",
        action="store_true",
        help="pUSD approve MAX to CTF + NegRisk exchanges, collateral adapters, Neg Risk Adapter",
    )
    parser.add_argument("--approve-sell", action="store_true", help="CTF setApprovalForAll for both exchanges")
    parser.add_argument(
        "--approve-collateral-adapters",
        action="store_true",
        help="pUSD approve MAX to CtfCollateralAdapter + NegRiskCtfCollateralAdapter + Neg Risk Adapter "
        "(when exchanges are already approved)",
    )
    parser.add_argument("--approve-all", action="store_true", help="--approve-buy AND --approve-sell")
    parser.add_argument("--sync-clob", action="store_true", help="CLOB update + read balance (GET only)")
    parser.add_argument(
        "--debug-clob-balance-allowance",
        action="store_true",
        help="SDK + raw HTTP: /balance-allowance/update and /balance-allowance (COLLATERAL, sig type 3)",
    )
    parser.add_argument("--token-id", type=str, default=None, help="With --sync-clob: CONDITIONAL asset")
    parser.add_argument(
        "--allow-wallet-mismatch",
        action="store_true",
        help="Continue if derived deposit wallet != expected constant",
    )
    parser.add_argument(
        "--deposit-wallet",
        type=str,
        default=None,
        help="Override expected deposit wallet address (default: built-in 0x4Fac…)",
    )
    parser.add_argument(
        "--transfer-native-usdc-to-owner",
        action="store_true",
        help="Plan WALLET batch: Polygon native USDC ERC20 transfer(deposit_wallet → owner EOA)",
    )
    parser.add_argument(
        "--transfer-amount",
        type=str,
        default=None,
        help="Native USDC raw amount (smallest units, integer string). Default: full balance on deposit wallet.",
    )
    parser.add_argument(
        "--get-bridge-deposit-address",
        action="store_true",
        help="POST https://bridge.polymarket.com/deposit — print EVM address to send native USDC for bridging",
    )
    args = parser.parse_args()
    _load_dotenv()

    if args.approve_all:
        args.approve_buy = True
        args.approve_sell = True

    if not any(
        [
            args.status,
            args.deploy,
            args.wrap_all_usdce,
            args.wrap_amount,
            args.approve_buy,
            args.approve_sell,
            args.approve_collateral_adapters,
            args.sync_clob,
            args.debug_clob_balance_allowance,
            args.transfer_native_usdc_to_owner,
            args.get_bridge_deposit_address,
        ]
    ):
        parser.print_help()
        print(
            "\nNo action flags passed. Typical read-only:\n"
            "  python scripts/setup_deposit_wallet_v2.py --status --sync-clob\n"
            "  python scripts/setup_deposit_wallet_v2.py --debug-clob-balance-allowance\n",
            file=sys.stderr,
        )
        return 2

    pk = _pk()
    if not pk:
        print("ERROR: set PRIVATE_KEY or TYREX_PRIVATE_KEY or POLYMARKET_PK", file=sys.stderr)
        return 2

    from eth_account import Account

    owner_eoa = to_checksum_address(Account.from_key(pk).address)
    rpc_url = _rpc_url()
    if (args.wrap_all_usdce or args.wrap_amount) and not rpc_url:
        print(
            "ERROR: RPC_URL required for wrap (read USDC.e balance on the deposit wallet)",
            file=sys.stderr,
        )
        return 2

    if args.transfer_native_usdc_to_owner and not rpc_url:
        print(
            "ERROR: RPC_URL required for --transfer-native-usdc-to-owner (read native USDC balance)",
            file=sys.stderr,
        )
        return 2

    deposit_expect = to_checksum_address(args.deposit_wallet or EXPECTED_DEPOSIT_WALLET)

    derived_early = _derived_deposit_wallet_from_pk(pk)

    debug_standalone = args.debug_clob_balance_allowance and not any(
        [
            args.status,
            args.deploy,
            args.wrap_all_usdce,
            args.wrap_amount,
            args.approve_buy,
            args.approve_sell,
            args.approve_collateral_adapters,
            args.sync_clob,
            args.transfer_native_usdc_to_owner,
            args.get_bridge_deposit_address,
        ]
    )
    if debug_standalone:
        if derived_early.lower() != deposit_expect.lower():
            if not args.allow_wallet_mismatch:
                print(
                    "ERROR: derived deposit wallet does not match expected / --deposit-wallet; "
                    "fix PRIVATE_KEY or override.",
                    file=sys.stderr,
                )
                return 3
        cmd_debug_clob_balance_allowance(derived_early, owner_eoa)
        return 0

    bridge_standalone = args.get_bridge_deposit_address and not any(
        [
            args.status,
            args.deploy,
            args.wrap_all_usdce,
            args.wrap_amount,
            args.approve_buy,
            args.approve_sell,
            args.approve_collateral_adapters,
            args.sync_clob,
            args.transfer_native_usdc_to_owner,
            args.debug_clob_balance_allowance,
        ]
    )
    if bridge_standalone:
        if derived_early.lower() != deposit_expect.lower():
            if not args.allow_wallet_mismatch:
                print(
                    "ERROR: derived deposit wallet does not match expected / --deposit-wallet; "
                    "fix PRIVATE_KEY or override.",
                    file=sys.stderr,
                )
                return 3
        print("=" * 72)
        print("Polymarket Bridge API (read-only)")
        print("=" * 72)
        print(f"owner_eoa (from PK)       : {owner_eoa}")
        print(f"deposit_wallet (derived) : {derived_early}")
        print()
        cmd_get_bridge_deposit_address(derived_early)
        return 0

    print("=" * 72)
    print("Polymarket V2 deposit-wallet setup (dry-run unless --execute for relayer txs)")
    print("=" * 72)
    print(f"owner_eoa (from PK)     : {owner_eoa}")
    print(f"expected deposit_wallet: {deposit_expect}")
    print(f"chain_id                : {CHAIN_ID}")
    print(f"relayer_url             : {_env_first('RELAYER_URL') or DEFAULT_RELAYER_URL}")
    print(
        f"rpc_url                 : {rpc_url or '(unset — optional; set for balances/allowances/code checks)'}"
    )
    print(f"execute_mutations       : {args.execute}")
    print()

    report = Report(owner_eoa=owner_eoa, deposit_wallet=deposit_expect)

    _ensure_relayer_sdk()
    from py_builder_relayer_client.client import RelayClient
    from py_builder_relayer_client.models import DepositWalletCall

    relayer = RelayClient(
        relayer_url=_env_first("RELAYER_URL") or DEFAULT_RELAYER_URL,
        chain_id=CHAIN_ID,
        private_key=pk,
        builder_config=_builder_config(),
    )
    derived = to_checksum_address(relayer.get_expected_deposit_wallet())
    print(f"derived deposit_wallet  : {derived}")
    if derived.lower() != deposit_expect.lower():
        msg = "Derived deposit wallet does not match expected constant / --deposit-wallet"
        if args.allow_wallet_mismatch:
            print(f"WARNING: {msg}; continuing (--allow-wallet-mismatch)")
            report.warnings.append(msg)
        else:
            print(f"ERROR: {msg}. Fix keys or pass --allow-wallet-mismatch.", file=sys.stderr)
            return 3
    deposit_wallet = to_checksum_address(derived)

    if args.debug_clob_balance_allowance:
        print()
        cmd_debug_clob_balance_allowance(deposit_wallet, owner_eoa)
        print()

    deployed_onchain = False
    if rpc_url:
        deployed_onchain = len(_eth_get_code(rpc_url, deposit_wallet)) > 0
    print(f"on-chain code at wallet : {'yes (deployed)' if deployed_onchain else 'no (not deployed or RPC error)'}")

    relayer_deployed = False
    try:
        relayer_deployed = relayer.get_deployed(deposit_wallet, None)
    except Exception as e:
        report.warnings.append(f"relayer get_deployed failed: {e}")
    print(f"relayer get_deployed()  : {relayer_deployed}")
    print()

    if deployed_onchain and not relayer_deployed:
        print(f"WARNING: {DEPLOY_RELAYER_MISMATCH_WARN}")
        report.warnings.append(DEPLOY_RELAYER_MISMATCH_WARN)

    if args.status and rpc_url:
        cmd_status(rpc_url, deposit_wallet, report)
    elif args.status and not rpc_url:
        print("--- On-chain status ---")
        print(
            "  SKIPPED: RPC_URL not set. Export a Polygon HTTPS endpoint to read balances, "
            "allowances, ERC1155 approvals, and contract code."
        )
        report.warnings.append("RPC_URL unset — on-chain snapshot skipped.")
    if args.deploy:
        print("--- WALLET-CREATE (deploy deposit wallet) ---")
        print(f"  type     : WALLET-CREATE")
        print(f"  from     : {owner_eoa}")
        print(f"  to       : {DEPOSIT_WALLET_FACTORY}")
        print(f"  factory  : {DEPOSIT_WALLET_FACTORY}")
        if deployed_onchain or relayer_deployed:
            print("  note     : Wallet appears deployed; relayer may reject duplicate deploy.")
        if args.execute:
            if deployed_onchain:
                print("  SKIP execute: already deployed on-chain")
            else:
                resp = relayer.deploy_deposit_wallet()
                print(f"  submitted transaction_id={resp.transaction_id}")
                row = resp.wait()
                print(f"  terminal relayer row: {json.dumps(row, default=str)[:500]}…")
                if rpc_url:
                    deployed_onchain = len(_eth_get_code(rpc_url, deposit_wallet)) > 0
        else:
            print("  [dry-run] pass --execute to submit WALLET-CREATE")

    # --- Wrap USDC.e → pUSD ---
    wrap_amt: int | None = None
    if rpc_url and (args.wrap_all_usdce or args.wrap_amount):
        bal_raw = _decode_uint256(_eth_call(rpc_url, USDC_E, _encode_balance_of(deposit_wallet)))
        if args.wrap_amount:
            wrap_amt = int(args.wrap_amount)
        else:
            wrap_amt = bal_raw
        print("--- WALLET batch: USDC.e approve + CollateralOnramp.wrap ---")
        print(f"  deposit_wallet (msg.sender for wrap): {deposit_wallet}")
        print(f"  USDC.e balance (raw)               : {bal_raw} ({_fmt_usd6(bal_raw)} USD)")
        print(f"  wrap amount (raw)                   : {wrap_amt}")
        if wrap_amt <= 0:
            print("  NOTE: nothing to wrap")
        else:
            ap_data = _encode_approve(COLLATERAL_ONRAMP, wrap_amt)
            w_data = _encode_wrap(USDC_E, deposit_wallet, wrap_amt)
            _print_call(
                "1",
                USDC_E,
                ap_data,
                "USDC.e.approve(CollateralOnramp, amount)",
                f"spender={COLLATERAL_ONRAMP}",
            )
            _print_call(
                "2",
                COLLATERAL_ONRAMP,
                w_data,
                "CollateralOnramp.wrap(USDC.e, deposit_wallet, amount)",
                f"asset={USDC_E}, to={deposit_wallet}, amount={wrap_amt}",
            )
            if args.execute:
                if not deployed_onchain:
                    print("ERROR: deploy wallet before wrap (--deploy --execute)", file=sys.stderr)
                    return 4
                if wrap_amt > bal_raw:
                    print(
                        f"ERROR: wrap amount {wrap_amt} exceeds USDC.e balance {bal_raw}",
                        file=sys.stderr,
                    )
                    return 5
                nonce = _wallet_nonce(relayer)
                dl = _deadline()
                print(f"  WALLET nonce (owner={owner_eoa}): {nonce}")
                print(f"  deadline (unix)                  : {dl}")
                calls = [
                    DepositWalletCall(target=USDC_E, value="0", data=ap_data),
                    DepositWalletCall(target=COLLATERAL_ONRAMP, value="0", data=w_data),
                ]
                resp = relayer.execute_deposit_wallet_batch(
                    calls, deposit_wallet, nonce, dl
                )
                print(f"  submitted transaction_id={resp.transaction_id}")
                resp.wait()
                print("  WALLET batch finished (poll terminal)")
            else:
                print("  [dry-run] pass --execute to submit WALLET batch")

    # --- Approvals ---
    if args.approve_buy or args.approve_sell or args.approve_collateral_adapters:
        calls: list[Any] = []
        print("--- WALLET batch: trading approvals ---")
        if args.approve_buy:
            for label, sp in (
                ("CTF Exchange", CTF_EXCHANGE),
                ("Neg Risk CTF Exchange", NEG_RISK_CTF_EXCHANGE),
                ("CtfCollateralAdapter", CTF_COLLATERAL_ADAPTER),
                ("NegRiskCtfCollateralAdapter", NEG_RISK_CTF_COLLATERAL_ADAPTER),
                ("Neg Risk Adapter", NEG_RISK_ADAPTER),
            ):
                d = _encode_approve(sp, MAX_UINT256)
                _print_call("pUSD approve", PUSD, d, f"pUSD.approve({label}, MAX)", f"spender={sp}")
                calls.append(DepositWalletCall(target=PUSD, value="0", data=d))
        elif args.approve_collateral_adapters:
            for label, sp in (
                ("CtfCollateralAdapter", CTF_COLLATERAL_ADAPTER),
                ("NegRiskCtfCollateralAdapter", NEG_RISK_CTF_COLLATERAL_ADAPTER),
                ("Neg Risk Adapter", NEG_RISK_ADAPTER),
            ):
                d = _encode_approve(sp, MAX_UINT256)
                _print_call("pUSD approve", PUSD, d, f"pUSD.approve({label}, MAX)", f"spender={sp}")
                calls.append(DepositWalletCall(target=PUSD, value="0", data=d))
        if args.approve_sell:
            for label, op in (
                ("CTF Exchange", CTF_EXCHANGE),
                ("Neg Risk CTF Exchange", NEG_RISK_CTF_EXCHANGE),
            ):
                d = _encode_set_approval_for_all(op, True)
                _print_call(
                    "CTF approval",
                    CONDITIONAL_TOKENS,
                    d,
                    f"ConditionalTokens.setApprovalForAll({label}, true)",
                    f"operator={op}",
                )
                calls.append(DepositWalletCall(target=CONDITIONAL_TOKENS, value="0", data=d))
        if args.execute:
            if not deployed_onchain:
                print("ERROR: deploy wallet before approvals", file=sys.stderr)
                return 4
            if not calls:
                print("ERROR: no approval calls selected", file=sys.stderr)
                return 6
            nonce = _wallet_nonce(relayer)
            dl = _deadline()
            print(f"  WALLET nonce: {nonce}  deadline: {dl}")
            resp = relayer.execute_deposit_wallet_batch(calls, deposit_wallet, nonce, dl)
            print(f"  submitted transaction_id={resp.transaction_id}")
            resp.wait()
        else:
            print("  [dry-run] pass --execute to submit")
        print(
            "  Next: python scripts/setup_deposit_wallet_v2.py --status --sync-clob"
        )

    if rpc_url and args.transfer_native_usdc_to_owner:
        if args.allow_wallet_mismatch:
            print(
                "ERROR: --transfer-native-usdc-to-owner cannot be used with --allow-wallet-mismatch.",
                file=sys.stderr,
            )
            return 7
        nat_cs = to_checksum_address(POLYGON_NATIVE_USDC)
        bal_raw = _decode_uint256(_eth_call(rpc_url, nat_cs, _encode_balance_of(deposit_wallet)))
        amt = int(args.transfer_amount) if args.transfer_amount else bal_raw
        xfer_data = _encode_erc20_transfer(owner_eoa, amt)
        print("--- WALLET batch: Polygon native USDC → owner EOA ---")
        print(f"  token (native USDC)       : {nat_cs}")
        print(f"  from (deposit_wallet)      : {deposit_wallet}")
        print(f"  to (owner_eoa)             : {owner_eoa}")
        print(f"  amount_raw                 : {amt}")
        print(f"  amount_human (~USD, 6dp)   : {_fmt_usd6(amt)}")
        print(f"  on-chain balance_raw       : {bal_raw}")
        print(f"  purpose                    : ERC20.transfer(recipient=owner_eoa, amount)")
        if bal_raw <= 0:
            print("ERROR: native USDC balance on deposit wallet is zero.", file=sys.stderr)
            return 8
        if amt <= 0:
            print("ERROR: transfer amount must be > 0.", file=sys.stderr)
            return 9
        if amt > bal_raw:
            print(f"ERROR: transfer amount {amt} exceeds on-chain balance {bal_raw}.", file=sys.stderr)
            return 10
        nonce_val: str | None = None
        dl_val: str | None = None
        if args.execute:
            nonce_val = _wallet_nonce(relayer)
            dl_val = _deadline()
            nonce_disp, dl_disp = nonce_val, dl_val
        else:
            nonce_disp = "(dry-run — pass --execute to fetch WALLET nonce from relayer)"
            dl_disp = "(dry-run — deadline chosen at execute, typically now + 600s)"
        print(f"  WALLET nonce (GET nonce for owner, type=WALLET): {nonce_disp}")
        print(f"  deadline (unix)                                : {dl_disp}")
        _print_call(
            "native-USDC-transfer",
            nat_cs,
            xfer_data,
            "Polygon native USDC.transfer(owner_eoa, amount)",
            f"recipient={owner_eoa}",
        )
        if args.execute:
            if not deployed_onchain:
                print(
                    "ERROR: deposit wallet has no contract code on-chain; cannot run WALLET batch.",
                    file=sys.stderr,
                )
                return 4
            assert nonce_val is not None and dl_val is not None
            calls = [DepositWalletCall(target=nat_cs, value="0", data=xfer_data)]
            resp = relayer.execute_deposit_wallet_batch(calls, deposit_wallet, nonce_val, dl_val)
            print(f"  submitted transaction_id={resp.transaction_id}")
            resp.wait()
            print("  WALLET batch finished (poll terminal)")
        else:
            print("  [dry-run] pass --execute to submit WALLET batch")

    if args.get_bridge_deposit_address:
        print()
        cmd_get_bridge_deposit_address(deposit_wallet)

    if args.sync_clob:
        print("--- CLOB balance sync (GET endpoints; not an on-chain tx) ---")
        try:
            cmd_sync_clob(deposit_wallet, report, args.token_id)
        except Exception as e:
            report.warnings.append(f"CLOB sync failed: {e}")
            print(f"ERROR: {e}", file=sys.stderr)

    # Final on-chain snapshot for operator report (same interpretation as Tyrex wallet_sync)
    if rpc_url:
        cmd_status(rpc_url, deposit_wallet, report)

    report.deposit_wallet = deposit_wallet

    print()
    print("=" * 72)
    print("OPERATOR REPORT")
    print("=" * 72)
    print(f"owner_eoa               : {report.owner_eoa}")
    print(f"deposit_wallet          : {deposit_wallet}")
    print(f"deployed (RPC code)     : {report.deployed}")
    print(f"pUSD balance (on-chain)   : {report.pusd_balance}")
    print(f"USDC.e balance (on-chain) : {report.usdce_balance}")
    print(f"Native USDC (informational): {report.native_usdc_balance}")
    print(f"pUSD allowance → CTF Exchange           : {report.pusd_allow_ctf}")
    print(f"pUSD allowance → Neg Risk CTF Exchange : {report.pusd_allow_neg}")
    print(f"pUSD allowance → CtfCollateralAdapter   : {report.pusd_allow_ctf_collateral_adapter}")
    print(
        f"pUSD allowance → NegRiskCtfCollateralAdapter : "
        f"{report.pusd_allow_neg_risk_ctf_collateral_adapter}"
    )
    print(f"pUSD allowance → Neg Risk Adapter        : {report.pusd_allow_neg_risk_adapter}")
    print(f"CTF isApprovedForAll      : {report.ctf_approved}")
    print(f"NegRisk isApprovedForAll   : {report.neg_ctf_approved}")
    print(f"CLOB collateral balance   : {report.clob_balance}")
    print(f"CLOB collateral allowance : {report.clob_allowance}")
    print()
    print("Recommended Tyrex .env:")
    print("  TYREX_SIGNATURE_TYPE=3")
    print(f"  TYREX_FUNDER={deposit_wallet}")
    print("  TYREX_CLOB_HOST=https://clob.polymarket.com")
    print("  TYREX_CHAIN_ID=137")
    print()

    try:
        bal_d = Decimal(report.clob_balance if report.clob_balance != "?" else "0")
        al_d = Decimal(report.clob_allowance if report.clob_allowance != "?" else "0")
        passes = bal_d > 0 and al_d > 0
        print(
            "Tyrex capital gate (from this report's CLOB collateral balance & allowance; "
            "run --sync-clob so values reflect GET /balance-allowance): "
            f"{'PASS' if passes else 'FAIL'}"
        )
    except Exception:
        print("Tyrex capital gate: UNKNOWN (parse error)")

    hints: list[str] = []
    pusd_r = Decimal(report.pusd_balance or "0")
    if rpc_url:
        usdce_r = Decimal(report.usdce_balance or "0")
        if pusd_r <= 0 and usdce_r > 0:
            hints.append(
                "pUSD is 0 but USDC.e on deposit wallet — run wrap batch (--wrap-all-usdce --execute) "
                "then --sync-clob."
            )
        if pusd_r <= 0 and usdce_r <= 0 and report.native_usdc_raw > 0:
            hints.append(
                "Native Polygon USDC on deposit wallet does not become CLOB collateral by itself — "
                "see NEXT STEPS (transfer to owner, bridge deposit address, then approvals)."
            )
        if pusd_r <= 0 and usdce_r <= 0 and report.native_usdc_raw <= 0:
            hints.append(
                "Fund deposit wallet: Polymarket expects **pUSD** on this address for CLOB buying power "
                "(USDC.e must be wrapped via CollateralOnramp through a WALLET batch)."
            )

    clob_b = report.clob_balance
    if rpc_url and pusd_r > 0 and clob_b in ("0", "0.000000", "?", ""):
        hints.append(
            "On-chain pUSD > 0 but CLOB balance missing/zero — run --sync-clob and ensure approvals; "
            "confirm API keys belong to this signer."
        )

    low_pusd_spenders = (
        Decimal(report.pusd_allow_ctf or "0") <= 0
        or Decimal(report.pusd_allow_neg or "0") <= 0
        or Decimal(report.pusd_allow_ctf_collateral_adapter or "0") <= 0
        or Decimal(report.pusd_allow_neg_risk_ctf_collateral_adapter or "0") <= 0
        or Decimal(report.pusd_allow_neg_risk_adapter or "0") <= 0
    )
    if low_pusd_spenders and pusd_r > 0:
        hints.append(
            "pUSD allowance missing for an exchange, collateral adapter, or Neg Risk Adapter — run "
            "`python scripts/setup_deposit_wallet_v2.py --approve-buy --execute` "
            "(or `--approve-collateral-adapters --execute` for adapter-only batch), "
            "then `python scripts/setup_deposit_wallet_v2.py --status --sync-clob`."
        )

    all_notes = report.warnings + hints
    if all_notes:
        print("\nNotes:")
        for w in all_notes:
            print(f"  - {w}")

    _print_next_step_commands(
        report=report,
        rpc_url=rpc_url or "",
        native_raw_balance=report.native_usdc_raw,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
