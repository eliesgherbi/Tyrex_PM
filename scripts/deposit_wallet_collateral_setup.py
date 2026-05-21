#!/usr/bin/env python3
"""Deposit-wallet collateral approvals + CLOB balance sync for ``signature_type=3``.

This follows Polymarket’s deposit-wallet flow:

* Submit a relayer ``WALLET`` batch that runs ``pUSD.approve(spender, MAX)``
  **from the deposit wallet** (gasless). Batch EIP-712 domain matches the
  Deposit Wallet guide (``name=DepositWallet``, ``verifyingContract=<deposit>``).
  Source: https://docs.polymarket.com/trading/deposit-wallets
* Call ``GET .../balance-allowance/update?asset_type=COLLATERAL&signature_type=3``
  via ``py-clob-client-v2`` (same doc — sync CLOB balances after funding / approvals).
* Print deposit wallet + sig=3 balance / allowance (parsed like Tyrex wallet sync).

The PyPI package ``py-builder-relayer-client==0.0.1`` in this repo’s environment is
**Safe/proxy oriented** and does **not** expose ``execute_deposit_wallet_batch`` /
``DepositWalletCall``. Relayer HTTP is implemented here using the documented JSON body
and Builder API HMAC headers from ``py-builder-signing-sdk`` (or Relayer API key
headers per OpenAPI).

Auth (pick **one**):

* **Builder API keys** (HMAC): ``POLYMARKET_BUILDER_API_KEY``,
  ``POLYMARKET_BUILDER_SECRET``, ``POLYMARKET_BUILDER_PASSPHRASE``
  (fallback: ``BUILDER_API_KEY``, ``BUILDER_SECRET``, ``BUILDER_PASS_PHRASE``).
  Headers: ``POLY_BUILDER_*`` — https://docs.polymarket.com/api-reference/relayer
* **Relayer API keys**: ``RELAYER_API_KEY``, ``RELAYER_API_KEY_ADDRESS``
  — same reference.

Env (trading wallet):

* ``POLYMARKET_PK`` — owner EOA private key (same as Tyrex live signer); **never printed**.
* ``POLYMARKET_FUNDER`` — deposit wallet address holding pUSD (checksum-insensitive).
* ``POLYMARKET_SIGNATURE_TYPE=3`` recommended before running so ``try_create_clob_client``
  matches verification reads.

Optional:

* ``RELAYER_URL`` — default ``https://relayer-v2.polymarket.com``
  (see https://docs.polymarket.com/developers/builders/relayer-client )
* ``TYREX_CHAIN_ID`` / ``POLYMARKET_CHAIN_ID`` — default ``137``

Usage::

    python scripts/deposit_wallet_collateral_setup.py
    python scripts/deposit_wallet_collateral_setup.py --dry-run --verbose
    python scripts/deposit_wallet_collateral_setup.py --skip-batch   # only sync + verify

Secrets stay in the environment; this script does **not** log keys or passphrases.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

import httpx
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import to_checksum_address

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# Polygon deposit-wallet factory (Polymarket deposit-wallet guide).
DEPOSIT_WALLET_FACTORY = "0x00000000000Fb5C9ADea0298D729a0CB3823Cc07"

# ERC20 approve(address,uint256)
APPROVE_SELECTOR = bytes.fromhex("095ea7b3")
MAX_UINT256 = 2**256 - 1


def _load_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=True)
    except ImportError:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
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


def _encode_approve(spender: str, amount: int = MAX_UINT256) -> str:
    sp = to_checksum_address(spender)
    body = abi_encode(["address", "uint256"], [sp, amount])
    return "0x" + (APPROVE_SELECTOR + body).hex()


def _approve_calls_for_polymarket_v2(chain_id: int) -> list[dict[str, str]]:
    """Return WALLET batch calls: pUSD approve for spenders Tyrex may bind against."""
    from py_clob_client_v2.config import get_contract_config

    cfg = get_contract_config(chain_id)
    pusd = to_checksum_address(cfg.collateral)
    spenders = [
        cfg.conditional_tokens,
        cfg.exchange_v2,
        cfg.neg_risk_exchange_v2,
        cfg.neg_risk_adapter,
    ]
    calls = []
    for s in spenders:
        calls.append(
            {
                "target": pusd,
                "value": "0",
                "data": _encode_approve(to_checksum_address(s)),
            }
        )
    return calls


def _builder_headers(method: str, path: str, body_str: str | None) -> dict[str, str]:
    try:
        from py_builder_signing_sdk.config import BuilderConfig
        from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
    except ImportError as e:  # pragma: no cover
        raise SystemExit(
            "Install builder signing SDK: pip install py-builder-signing-sdk\n"
            f"Import error: {e}"
        ) from e

    key = _env_first("POLYMARKET_BUILDER_API_KEY", "BUILDER_API_KEY")
    secret = _env_first("POLYMARKET_BUILDER_SECRET", "BUILDER_SECRET")
    passphrase = _env_first(
        "POLYMARKET_BUILDER_PASSPHRASE",
        "BUILDER_PASS_PHRASE",
        "BUILDER_PASSPHRASE",
    )
    if not (key and secret and passphrase):
        raise SystemExit(
            "Builder API credentials missing. Set POLYMARKET_BUILDER_API_KEY, "
            "POLYMARKET_BUILDER_SECRET, POLYMARKET_BUILDER_PASSPHRASE "
            "(or BUILDER_* aliases)."
        )

    cfg = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=key,
            secret=secret,
            passphrase=passphrase,
        )
    )
    payload = cfg.generate_builder_headers(method, path, body_str)
    if payload is None:
        raise SystemExit("Could not generate POLY_BUILDER_* headers")
    return payload.to_dict()


def _relayer_key_headers() -> dict[str, str]:
    rk = _env_first("RELAYER_API_KEY")
    ra = _env_first("RELAYER_API_KEY_ADDRESS")
    if not (rk and ra):
        raise SystemExit(
            "Set RELAYER_API_KEY and RELAYER_API_KEY_ADDRESS, "
            "or use Builder API credentials instead."
        )
    return {
        "RELAYER_API_KEY": rk,
        "RELAYER_API_KEY_ADDRESS": to_checksum_address(ra),
    }


def _auth_headers(method: str, request_path: str, body_str: str | None) -> dict[str, str]:
    if _env_first("POLYMARKET_BUILDER_API_KEY", "BUILDER_API_KEY"):
        return _builder_headers(method, request_path, body_str)
    if _env_first("RELAYER_API_KEY"):
        return _relayer_key_headers()
    raise SystemExit(
        "No relayer auth configured. Provide either Builder API vars "
        "(POLYMARKET_BUILDER_*) or RELAYER_API_KEY + RELAYER_API_KEY_ADDRESS."
    )


def _sign_wallet_batch(
    *,
    owner_key: str,
    chain_id: int,
    deposit_wallet: str,
    nonce: int,
    deadline: int,
    calls: list[dict[str, str]],
) -> str:
    """Return 0x hex ECDSA signature over DepositWallet Batch EIP-712."""
    dep = to_checksum_address(deposit_wallet)
    calls_msg = []
    for c in calls:
        data_hex = c["data"]
        if not data_hex.startswith("0x"):
            data_hex = "0x" + data_hex
        calls_msg.append(
            {
                "target": to_checksum_address(c["target"]),
                "value": int(c["value"]),
                "data": bytes.fromhex(data_hex[2:]),
            }
        )

    typed_data: dict[str, Any] = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Call": [
                {"name": "target", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "data", "type": "bytes"},
            ],
            "Batch": [
                {"name": "wallet", "type": "address"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
                {"name": "calls", "type": "Call[]"},
            ],
        },
        "primaryType": "Batch",
        "domain": {
            "name": "DepositWallet",
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": dep,
        },
        "message": {
            "wallet": dep,
            "nonce": nonce,
            "deadline": deadline,
            "calls": calls_msg,
        },
    }

    encoded = encode_typed_data(full_message=typed_data)
    signed = Account.sign_message(encoded, private_key=owner_key)
    return signed.signature.hex() if signed.signature.hex().startswith("0x") else "0x" + signed.signature.hex()


def _poll_transaction(
    relayer_url: str,
    tx_id: str,
    header_fn: Callable[[str, str, str | None], dict[str, str]],
) -> dict[str, Any]:
    """Poll GET /transaction until terminal success. ``header_fn(method,path,body)->headers``."""
    url = f"{relayer_url}/transaction"
    for i in range(45):
        hdr = header_fn("GET", "/transaction", None)
        r = httpx.get(url, params={"id": tx_id}, headers=hdr, timeout=30.0)
        r.raise_for_status()
        body = r.json()
        rows = body if isinstance(body, list) else body.get("transactions") or []
        if isinstance(rows, list) and rows:
            row = rows[0]
            state = row.get("state")
            if state in ("STATE_CONFIRMED", "STATE_MINED"):
                return row
            if state in ("STATE_FAILED", "STATE_INVALID"):
                raise SystemExit(f"Relayer transaction terminal failure: {row}")
        time.sleep(2.0)
        if i % 5 == 0:
            print(f"   … polling relayer tx {tx_id} ({i + 1}/45)")
    raise SystemExit("Timed out waiting for relayer STATE_CONFIRMED/MINED")


def _parse_balance_allowance(bal: dict[str, Any]) -> tuple[str, str]:
    """Match Tyrex ``clob_wallet_sync._v2_balance_to_usd`` scaling (6 decimals)."""
    from decimal import Decimal

    allowances_dict = bal.get("allowances")
    is_v2 = isinstance(allowances_dict, dict) and bool(allowances_dict)
    raw_bal = bal.get("balance")
    if raw_bal is None:
        raw_bal = bal.get("available")
    scale = Decimal(10) ** 6

    def dec(x: Any) -> Decimal:
        try:
            return Decimal(str(x))
        except Exception:
            return Decimal(0)

    if raw_bal is None:
        b_out = "?"
    else:
        v = dec(raw_bal)
        b_out = str(v / scale) if is_v2 else str(v)

    if is_v2:
        try:
            min_raw = min(dec(v) for v in allowances_dict.values())
            a_out = str(min_raw / scale)
        except ValueError:
            a_out = "?"
    else:
        legacy = bal.get("allowance") or bal.get("allowance_balance")
        a_out = str(dec(legacy)) if legacy is not None else "?"

    return b_out, a_out


def _clob_client_pkg_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("py-clob-client-v2")
    except Exception:
        return None


def _verify_sig3_balances(*, verbose: bool = False, ran_relayer_batch: bool | None = None) -> None:
    from py_clob_client_v2 import AssetType, BalanceAllowanceParams

    from tyrex_pm.venue.polymarket.clob_env import try_create_clob_client, v2_sdk_version

    client = try_create_clob_client()
    if client is None:
        raise SystemExit("try_create_clob_client() returned None; check POLYMARKET_PK / API creds")
    dep = to_checksum_address(_env_first("POLYMARKET_FUNDER", "TYREX_FUNDER"))
    sig_raw = (
        os.environ.get("TYREX_SIGNATURE_TYPE", "").strip()
        or os.environ.get("POLYMARKET_SIGNATURE_TYPE", "").strip()
        or "0"
    )
    print("\n--- CLOB verification (signature_type from env for SDK builder)")
    v_inline = v2_sdk_version()
    v_meta = _clob_client_pkg_version()
    print(
        "py-clob-client-v2 version:",
        v_inline or v_meta or "(not exposed by package — upgrade if unknown)",
    )
    print(f"deposit_wallet (POLYMARKET_FUNDER): {dep}")
    print(f"TYREX_/POLYMARKET_SIGNATURE_TYPE effective int on client: {int(sig_raw)}")
    try:
        clob_signer = to_checksum_address(client.get_address())
        print(f"CLOB client signer address (from PK): {clob_signer}")
    except Exception:
        print("CLOB client signer address: (could not read)")

    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    raw_get = client.get_balance_allowance(params)
    if verbose:
        print("\n[verbose] Raw GET /balance-allowance BEFORE update:")
        print(json.dumps(raw_get, indent=2, default=str))
    b0, a0 = _parse_balance_allowance(raw_get if isinstance(raw_get, dict) else {})
    print(f"GET /balance-allowance (collateral) BEFORE update — balance={b0} allowance(min)={a0}")

    client.update_balance_allowance(params)
    raw_after = client.get_balance_allowance(params)
    if verbose:
        print("\n[verbose] Raw GET /balance-allowance AFTER update:")
        print(json.dumps(raw_after, indent=2, default=str))
    b1, a1 = _parse_balance_allowance(raw_after if isinstance(raw_after, dict) else {})
    print(f"GET /balance-allowance (collateral) AFTER update — balance={b1} allowance(min)={a1}")

    if b1 == "0" or b1 == "?":
        lines = [
            "",
            "Note: CLOB still reports zero/unknown collateral under signature_type=3.",
            "Checks that usually explain this (see deposit-wallet guide):",
            "  • POLYMARKET_FUNDER must be Polymarket’s deposit wallet for this owner/signer —",
            "    on-chain pUSD on an arbitrary contract is not enough if CLOB does not map it.",
            "  • L2 API credentials must belong to the same POLYMARKET_PK used as signer",
            "    (derive/create API key with that key, or set POLYMARKET_API_* for that identity).",
            "  • Keep POLYMARKET_SIGNATURE_TYPE=3 so balance queries match deposit-wallet mode.",
        ]
        if ran_relayer_batch is False:
            lines.extend(
                [
                    "  • You ran --dry-run: relayer WALLET approvals were NOT submitted —",
                    "    allowance may stay 0 until you run without --dry-run (after fixing auth).",
                    "    Balance can still be non-zero without approvals; both reported as 0 here",
                    "    points to wallet/API mapping, not only approvals.",
                ]
            )
        lines.append(
            "Doc: https://docs.polymarket.com/trading/deposit-wallets (sync + POLY_1271)."
        )
        print("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description="Deposit wallet pUSD approvals + CLOB sync")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned batch + typed-data summary without calling relayer",
    )
    parser.add_argument(
        "--skip-batch",
        action="store_true",
        help="Skip WALLET relayer submit; only run balance-allowance update + verify",
    )
    parser.add_argument(
        "--deadline-offset",
        type=int,
        default=600,
        help="Batch EIP-712 deadline = now + this many seconds (default 600)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print raw GET /balance-allowance JSON (no secrets)",
    )
    args = parser.parse_args()

    _load_dotenv()

    pk = _env_first("TYREX_PRIVATE_KEY", "POLYMARKET_PK")
    if not pk:
        raise SystemExit("POLYMARKET_PK (or TYREX_PRIVATE_KEY) is required")
    dep_raw = _env_first("POLYMARKET_FUNDER", "TYREX_FUNDER")
    if not dep_raw:
        raise SystemExit("POLYMARKET_FUNDER (or TYREX_FUNDER) deposit wallet address is required")

    chain_id = int(_env_first("TYREX_CHAIN_ID", "POLYMARKET_CHAIN_ID") or "137")
    relayer_url = _env_first("RELAYER_URL").rstrip("/") or "https://relayer-v2.polymarket.com"
    deposit_wallet = to_checksum_address(dep_raw)
    owner = Account.from_key(pk)
    owner_addr = to_checksum_address(owner.address)

    print("Owner EOA (from PK, not secret):", owner_addr)
    print("Deposit wallet (POLYMARKET_FUNDER):", deposit_wallet)
    print("Relayer URL:", relayer_url)

    calls = _approve_calls_for_polymarket_v2(chain_id)
    deadline = int(time.time()) + max(60, args.deadline_offset)

    if args.dry_run:
        print("\n[dry-run] Would submit WALLET batch with calls:")
        print(json.dumps(calls, indent=2))
        print(f"[dry-run] deadline={deadline} factory_to={DEPOSIT_WALLET_FACTORY}")
        _verify_sig3_balances(verbose=args.verbose, ran_relayer_batch=False)
        return

    if args.skip_batch:
        print("\n--skip-batch: skipping relayer WALLET submit")
        _verify_sig3_balances(verbose=args.verbose, ran_relayer_batch=None)
        return

    def header_fn(method: str, path: str, body: str | None) -> dict[str, str]:
        return _auth_headers(method, path, body)

    nonce_url = f"{relayer_url}/nonce"
    nonce_q = {"address": owner_addr, "type": "WALLET"}
    hdr_get = header_fn("GET", "/nonce", None)
    nr = httpx.get(nonce_url, params=nonce_q, headers=hdr_get, timeout=30.0)
    if nr.status_code != 200:
        raise SystemExit(f"GET /nonce failed {nr.status_code}: {nr.text[:500]}")
    nonce_payload = nr.json()
    if nonce_payload is None or nonce_payload.get("nonce") is None:
        raise SystemExit(f"Unexpected /nonce payload: {nonce_payload}")
    nonce_int = int(str(nonce_payload["nonce"]))

    sig_hex = _sign_wallet_batch(
        owner_key=pk,
        chain_id=chain_id,
        deposit_wallet=deposit_wallet,
        nonce=nonce_int,
        deadline=deadline,
        calls=calls,
    )

    submit_body = {
        "type": "WALLET",
        "from": owner_addr,
        "to": DEPOSIT_WALLET_FACTORY,
        "nonce": str(nonce_int),
        "signature": sig_hex,
        "depositWalletParams": {
            "depositWallet": deposit_wallet,
            "deadline": str(deadline),
            "calls": calls,
        },
    }
    body_str = json.dumps(submit_body, separators=(",", ":"), ensure_ascii=False)
    hdr_post = header_fn("POST", "/submit", body_str)
    pr = httpx.post(
        f"{relayer_url}/submit",
        content=body_str,
        headers={**hdr_post, "Content-Type": "application/json"},
        timeout=60.0,
    )
    if pr.status_code != 200:
        raise SystemExit(f"POST /submit failed {pr.status_code}: {pr.text[:800]}")
    resp = pr.json()
    tx_id = resp.get("transactionID") or resp.get("transactionId")
    if not tx_id:
        raise SystemExit(f"Unexpected /submit response: {resp}")
    print(f"\nRelayer accepted WALLET batch: transactionID={tx_id} state={resp.get('state')}")
    print("Polling relayer until mined/confirmed…")
    row = _poll_transaction(relayer_url, tx_id, header_fn)
    print("Relayer row:", json.dumps(row, indent=2, default=str)[:2000])

    print("\n--- Balance cache refresh + verification")
    _verify_sig3_balances(verbose=args.verbose, ran_relayer_batch=True)

if __name__ == "__main__":
    main()
