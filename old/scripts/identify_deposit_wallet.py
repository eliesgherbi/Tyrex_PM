"""Polymarket wallet helper: deterministic **deposit wallet** + optional CLOB probe.

Derives the ERC-1967 **deposit wallet** (POLY_1271 / type ``3``) for the owner
EOA — useful when the venue says ``maker address not allowed, please use the
deposit wallet flow`` ([Deposit wallets guide](
https://docs.polymarket.com/trading/deposit-wallets)).

**Default account mode (Gmail / Gnosis Safe, type ``2``):** use
``--probe-clob`` to hit ``/balance-allowance`` with **your env**
``POLYMARKET_SIGNATURE_TYPE`` + ``POLYMARKET_FUNDER`` (same as Tyrex
``try_create_clob_client``). **No forced type-3 probe.**

**Deposit-wallet-only diagnostic:** ``--probe-deposit-wallet`` probes CLOB under
``signature_type=3`` with the **derived** deposit address (migration / API-only
path). **No order** and no relayer tx from this script.

Usage::

    pip install -U py-clob-client-v2 "py-builder-relayer-client>=0.0.2rc1"
    python scripts/identify_deposit_wallet.py
    python scripts/identify_deposit_wallet.py --probe-clob
    python scripts/identify_deposit_wallet.py --probe-deposit-wallet

Env:

* ``TYREX_PRIVATE_KEY`` or ``POLYMARKET_PK`` (required)
* ``TYREX_SIGNATURE_TYPE`` / ``POLYMARKET_SIGNATURE_TYPE`` — used by ``--probe-clob`` (default ``0`` if unset)
* ``POLYMARKET_FUNDER`` / ``TYREX_FUNDER`` — required when ``signature_type != 0`` for ``--probe-clob``
* ``TYREX_CLOB_HOST``, ``TYREX_CHAIN_ID`` / ``POLYMARKET_CHAIN_ID``
* ``POLYMARKET_API_*`` — optional; if all set, used for probe; else derive once

Secrets are never echoed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


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


def _present(flag: bool) -> str:
    return "<set>" if flag else "<unset>"


def _checksum(addr: str) -> str:
    from eth_utils import to_checksum_address

    return to_checksum_address(addr)


def _parse_balance_allowance(bal: dict[str, Any]) -> tuple[str, str, dict[str, str]]:
    """Match Tyrex ``clob_wallet_sync._v2_balance_to_usd`` scaling (6 decimals).

    Returns ``(balance_str, min_allowance_str, allowance_breakdown)``.
    """
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

    breakdown: dict[str, str] = {}
    if is_v2:
        try:
            min_raw = min(dec(v) for v in allowances_dict.values())
            a_out = str(min_raw / scale)
        except ValueError:
            a_out = "?"
        for k, v in allowances_dict.items():
            breakdown[str(k)] = str(dec(v) / scale)
    else:
        legacy = bal.get("allowance") or bal.get("allowance_balance")
        a_out = str(dec(legacy)) if legacy is not None else "?"

    return b_out, a_out, breakdown


def _derive_deposit_wallet(owner_addr: str, chain_id: int) -> str:
    """Run ``py-builder-relayer-client``'s deterministic derivation."""
    try:
        from py_builder_relayer_client.builder.derive import derive_deposit_wallet
        from py_builder_relayer_client.config import (
            get_contract_config,
            is_deposit_wallet_config_valid,
        )
    except ImportError as e:
        raise SystemExit(
            "py-builder-relayer-client>=0.0.2rc1 is required.\n"
            "  pip install -U 'py-builder-relayer-client>=0.0.2rc1'\n"
            f"Import error: {e}"
        ) from e

    cfg = get_contract_config(chain_id)
    if not is_deposit_wallet_config_valid(cfg):
        raise SystemExit(
            f"chain {chain_id} has no deposit-wallet factory/implementation configured "
            "in py-builder-relayer-client; only Polygon mainnet (137) and Amoy (80002) "
            "are supported by Polymarket's docs"
        )
    return _checksum(
        derive_deposit_wallet(
            owner_addr,
            cfg.deposit_wallet_factory,
            cfg.deposit_wallet_implementation,
        )
    )


def _probe_clob_balance_allowance(
    *,
    pk: str,
    host: str,
    chain_id: int,
    signature_type: int,
    funder: str | None,
    have_env_creds: bool,
) -> dict[str, Any]:
    """Build a ClobClient with the given mode and read collateral via update+GET.

    ``signature_type`` / ``funder`` match :func:`tyrex_pm.venue.polymarket.clob_env.try_create_clob_client`.
    For EOA mode (``0``), ``funder`` may be ``None``.
    """
    try:
        from py_clob_client_v2 import (
            ApiCreds,
            AssetType,
            BalanceAllowanceParams,
            ClobClient,
        )
    except ImportError as e:
        raise SystemExit(
            "py-clob-client-v2 is required for --probe-clob.\n"
            "  pip install -U py-clob-client-v2\n"
            f"Import error: {e}"
        ) from e

    creds = None
    if have_env_creds:
        creds = ApiCreds(
            api_key=os.environ["POLYMARKET_API_KEY"].strip(),
            api_secret=os.environ["POLYMARKET_API_SECRET"].strip(),
            api_passphrase=os.environ["POLYMARKET_PASSPHRASE"].strip(),
        )

    client = ClobClient(
        host,
        chain_id=chain_id,
        key=pk,
        signature_type=signature_type,
        funder=funder,
    )
    if creds is None:
        from tyrex_pm.venue.polymarket.clob_env import _derive_or_create_api_key

        creds = _derive_or_create_api_key(client)
        if creds is None:
            raise SystemExit("could not derive CLOB API credentials for --probe-clob")
    client.set_api_creds(creds)

    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    update_resp = client.update_balance_allowance(params)
    get_resp = client.get_balance_allowance(params)
    return {
        "update": update_resp if isinstance(update_resp, dict) else {"raw": str(update_resp)},
        "get": get_resp if isinstance(get_resp, dict) else {"raw": str(get_resp)},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Derive the expected Polymarket deposit wallet for the configured signer."
    )
    parser.add_argument(
        "--probe-clob",
        action="store_true",
        help="CLOB /balance-allowance/update + GET using env TYREX_SIGNATURE_TYPE / "
             "POLYMARKET_SIGNATURE_TYPE and POLYMARKET_FUNDER / TYREX_FUNDER (same as Tyrex).",
    )
    parser.add_argument(
        "--probe-deposit-wallet",
        action="store_true",
        help="CLOB probe under signature_type=3 with the *derived* deposit wallet only "
             "(optional migration path; not your Gmail/Safe type-2 mode).",
    )
    parser.add_argument(
        "--legacy-funder",
        default=None,
        help="Override the Settings/Profile legacy Safe address printed for diff "
             "(default: POLYMARKET_FUNDER / TYREX_FUNDER from env).",
    )
    args = parser.parse_args()

    _load_dotenv()

    from tyrex_pm.venue.polymarket.clob_env import resolve_clob_host

    pk = _env_first("TYREX_PRIVATE_KEY", "POLYMARKET_PK")
    if not pk:
        print(
            "ERROR: missing private key. Set TYREX_PRIVATE_KEY or POLYMARKET_PK "
            "in the environment / .env (never logged).",
            file=sys.stderr,
        )
        return 2

    try:
        from eth_account import Account
    except ImportError as e:
        print(
            "ERROR: eth-account is required (transitive dep of py-clob-client-v2). "
            "pip install eth-account",
            file=sys.stderr,
        )
        print(f"Import error: {e}", file=sys.stderr)
        return 2

    try:
        owner = Account.from_key(pk)
    except Exception as e:
        print(
            "ERROR: could not load owner key from TYREX_PRIVATE_KEY/POLYMARKET_PK "
            "(check 0x-prefixed hex, length 32 bytes).",
            file=sys.stderr,
        )
        print(f"Detail: {type(e).__name__}", file=sys.stderr)
        return 2
    owner_addr = _checksum(owner.address)

    host = resolve_clob_host()
    chain_id = int(_env_first("TYREX_CHAIN_ID", "POLYMARKET_CHAIN_ID") or "137")

    legacy_funder_raw = (
        args.legacy_funder
        or _env_first("POLYMARKET_FUNDER", "TYREX_FUNDER")
        or ""
    )
    legacy_funder = _checksum(legacy_funder_raw) if legacy_funder_raw else None

    deposit_wallet = _derive_deposit_wallet(owner_addr, chain_id)

    api_key_set = bool(_env_first("POLYMARKET_API_KEY"))
    api_secret_set = bool(_env_first("POLYMARKET_API_SECRET"))
    api_passphrase_set = bool(_env_first("POLYMARKET_PASSPHRASE"))
    have_env_creds = api_key_set and api_secret_set and api_passphrase_set

    print("=" * 70)
    print("Polymarket deposit-wallet identifier")
    print("=" * 70)
    print(f"chain_id              : {chain_id}")
    print(f"clob_host             : {host}")
    print(f"owner_eoa             : {owner_addr}   (from PK; secret not printed)")
    print(f"polymarket_api_key    : {_present(api_key_set)}")
    print(f"polymarket_api_secret : {_present(api_secret_set)}")
    print(f"polymarket_passphrase : {_present(api_passphrase_set)}")
    print()
    print(f"derived_deposit_wallet: {deposit_wallet}")
    if legacy_funder is not None:
        same = legacy_funder.lower() == deposit_wallet.lower()
        print(f"legacy_settings_funder: {legacy_funder}")
        print(f"deposit == legacy?    : {'YES (same address)' if same else 'NO (different)'}")
    else:
        print("legacy_settings_funder: <not provided in env / --legacy-funder>")

    cfg_sig = _env_first("TYREX_SIGNATURE_TYPE", "POLYMARKET_SIGNATURE_TYPE").strip() or "0"
    cfg_funder = _env_first("POLYMARKET_FUNDER", "TYREX_FUNDER").strip() or None
    print()
    print("Tyrex-style env (for Gmail/Safe or EOA — matches try_create_clob_client):")
    print(f"    POLYMARKET_SIGNATURE_TYPE={cfg_sig}")
    print(
        f"    POLYMARKET_FUNDER={cfg_funder or '(none — required if signature_type is 1/2/3)'}"
    )
    print("  For POLY_1271-only *deposit-wallet* API migration, also compare:")
    print(f"    derived deposit (type 3 funder): {deposit_wallet}")
    print("    https://docs.polymarket.com/trading/deposit-wallets")
    print("Notes: TYREX_* wins over POLYMARKET_* when both are set.")

    def _run_probe(sig_t: int, fund: str | None, label: str) -> int | None:
        print()
        print(f"{label}")
        print("(no order is placed)")
        try:
            probe = _probe_clob_balance_allowance(
                pk=pk,
                host=host,
                chain_id=chain_id,
                signature_type=sig_t,
                funder=fund,
                have_env_creds=have_env_creds,
            )
        except SystemExit:
            raise
        except Exception as e:
            print(f"  CLOB probe failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 3

        print("\nUPDATE response (raw):")
        print(json.dumps(probe["update"], indent=2, default=str))
        print("\nGET response (raw):")
        print(json.dumps(probe["get"], indent=2, default=str))

        b, a_min, breakdown = _parse_balance_allowance(
            probe["get"] if isinstance(probe["get"], dict) else {}
        )
        print(f"\nparsed: balance={b}  allowance(min)={a_min}")
        if breakdown:
            print("allowance breakdown (per V2 contract):")
            for k, v in breakdown.items():
                print(f"  {k} -> {v}")
        return None

    if args.probe_clob:
        try:
            sig_t = int(cfg_sig)
        except ValueError:
            print(
                f"ERROR: POLYMARKET_SIGNATURE_TYPE / TYREX_SIGNATURE_TYPE must be an integer; got {cfg_sig!r}",
                file=sys.stderr,
            )
            return 2
        if sig_t != 0 and not cfg_funder:
            print(
                "ERROR: --probe-clob requires POLYMARKET_FUNDER (or TYREX_FUNDER) when "
                f"signature_type is non-zero (got signature_type={sig_t}).",
                file=sys.stderr,
            )
            return 2
        err = _run_probe(
            sig_t,
            cfg_funder if cfg_funder else None,
            f"--probe-clob: CLOB collateral for signature_type={sig_t} "
            f"funder={cfg_funder or '(EOA)'}",
        )
        if err is not None:
            return err

    if args.probe_deposit_wallet:
        if have_env_creds:
            try:
                env_sig = int(cfg_sig)
            except ValueError:
                env_sig = -1
            if env_sig != 3:
                print(
                    "NOTE: POLYMARKET_API_* is set while env signature_type is not 3. "
                    "L2 creds are usually bound to the L1 path used at creation (often "
                    "your live type-2 Safe). This type-3 probe may not reflect Polymarket's "
                    "view for the deposit wallet; unset POLYMARKET_API_* / regenerate for "
                    "type 3 + deposit funder if isolating POLY_1271.",
                    file=sys.stderr,
                )
        err = _run_probe(
            3,
            deposit_wallet,
            "--probe-deposit-wallet: CLOB collateral for signature_type=3 (POLY_1271) "
            f"funder={deposit_wallet}",
        )
        if err is not None:
            return err

    return 0


if __name__ == "__main__":
    sys.exit(main())
