#!/usr/bin/env python3
"""
Milestone v1.00: verify Polymarket L1/L2 credential path + one L2 read.

Configuration precedence (highest first):
  1. Variables already set in the process environment (shell export, CI, etc.).
  2. Variables from a `.env` file — only for keys *not* already set (python-dotenv
     default: load_dotenv(..., override=False)).

`.env` resolution order:
  - If TYREX_PM_DOTENV is set: load that file path only (must exist if set).
  - Else: load `<repo_root>/.env` if the file exists.

Secrets (PK, API secret, passphrase) are never printed. See the runbook for key names.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv_files() -> list[Path]:
    """
    Merge `.env` into os.environ without overriding existing variables.
    Returns list of paths loaded (for status line).
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        print(
            "ERROR: python-dotenv not installed. Run: pip install -e .",
            file=sys.stderr,
        )
        sys.exit(1)

    loaded: list[Path] = []
    custom = os.environ.get("TYREX_PM_DOTENV")
    if custom:
        path = Path(custom).expanduser()
        if not path.is_file():
            print(f"ERROR: TYREX_PM_DOTENV points to missing file: {path}", file=sys.stderr)
            sys.exit(1)
        load_dotenv(path, override=False)
        loaded.append(path)
        return loaded

    default = REPO_ROOT / ".env"
    if default.is_file():
        load_dotenv(default, override=False)
        loaded.append(default)
    return loaded


def _mask(value: str, *, keep: int = 6) -> str:
    if not value or len(value) <= keep:
        return "***"
    return f"{value[:keep]}…"


def main() -> int:
    loaded = _load_dotenv_files()
    if loaded:
        print("Config: merged .env (existing process env wins for each key; .env fills gaps):")
        for path in loaded:
            print(f"  {path}")
    else:
        print(
            "Config: no .env loaded (using process environment only). "
            f"Expected file: {REPO_ROOT / '.env'} or set TYREX_PM_DOTENV.",
        )

    pk = os.environ.get("POLYMARKET_PK")
    if not pk:
        print(
            "ERROR: POLYMARKET_PK is not set (after .env merge). "
            "Set it in the environment or in .env — never commit it.",
            file=sys.stderr,
        )
        return 1

    host = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
    chain_id = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
    funder = os.environ.get("POLYMARKET_FUNDER")

    if sig_type in (1, 2) and not funder:
        print(
            "ERROR: POLYMARKET_FUNDER is required for signature_type 1 or 2 "
            "(set in shell or .env).",
            file=sys.stderr,
        )
        return 1

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
    except ImportError:
        print("ERROR: py-clob-client not installed. Run: pip install -e .", file=sys.stderr)
        return 1

    api_key = os.environ.get("POLYMARKET_API_KEY")
    api_secret = os.environ.get("POLYMARKET_API_SECRET")
    passphrase = os.environ.get("POLYMARKET_PASSPHRASE")

    if (api_key or api_secret or passphrase) and not (api_key and api_secret and passphrase):
        print(
            "ERROR: L2 env incomplete. Provide all three or none:\n"
            "  POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_PASSPHRASE\n"
            "If none are set, L2 credentials are derived via L1 using POLYMARKET_PK.",
            file=sys.stderr,
        )
        return 1

    if api_key and api_secret and passphrase:
        from py_clob_client.clob_types import ApiCreds

        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=passphrase,
        )
        print("Using L2 credentials from env (.env and/or shell); api_key prefix:", _mask(api_key))
    else:
        print("Deriving L2 API credentials via L1 (create_or_derive_api_creds)…")
        temp = ClobClient(host, key=pk, chain_id=chain_id)
        creds = temp.create_or_derive_api_creds()
        print("Derived api_key prefix:", _mask(creds.api_key))

    kwargs: dict = {
        "host": host,
        "key": pk,
        "chain_id": chain_id,
        "creds": creds,
    }
    if funder:
        kwargs["signature_type"] = sig_type
        kwargs["funder"] = funder
    elif sig_type != 0:
        print("ERROR: funder missing but signature_type != 0.", file=sys.stderr)
        return 1
    else:
        kwargs["signature_type"] = 0

    client = ClobClient(**kwargs)

    print("Calling get_balance_allowance(COLLATERAL)…")
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
    result = client.get_balance_allowance(params)

    if not isinstance(result, dict):
        print(f"ERROR: Unexpected response type {type(result).__name__!r}", file=sys.stderr)
        return 1

    # Helps when API shape differs by client/version (never print full payload if it could grow).
    print("Response keys:", ", ".join(sorted(result.keys())))

    bal = result.get("balance")
    has_allowance_data = "allowance" in result or "allowances" in result

    print("OK: L2-authenticated read succeeded.")
    print("L2 method: get_balance_allowance")
    print(
        "Fields: balance=",
        "present" if "balance" in result else "absent",
        ", allowance(s)=",
        "present" if has_allowance_data else "absent",
        sep="",
    )

    def _print_amount(label: str, value: object) -> None:
        if value is None:
            print(f"{label}: (not returned by API for this call)")
            return
        s = str(value)
        if len(s) > 12:
            print(f"{label} (truncated): {s[:12]}…")
        else:
            print(f"{label}: {value}")

    def _print_allowances(label: str, obj: object) -> None:
        """Polymarket often returns `allowances` (plural); keep output short."""
        if isinstance(obj, dict):
            n = len(obj)
            print(f"{label}: object ({n} top-level key(s))")
            for i, (key, val) in enumerate(obj.items()):
                if i >= 5:
                    print(f"  … and {n - 5} more key(s)")
                    break
                vs = str(val)
                if len(vs) > 20:
                    vs = vs[:20] + "…"
                print(f"  {key}: {vs}")
        elif isinstance(obj, list):
            print(f"{label}: array ({len(obj)} item(s))")
            for i, item in enumerate(obj[:3]):
                line = str(item)
                if len(line) > 72:
                    line = line[:72] + "…"
                print(f"  [{i}]: {line}")
            if len(obj) > 3:
                print(f"  … and {len(obj) - 3} more")
        else:
            _print_amount(label, obj)

    _print_amount("balance", bal)
    if "allowance" in result:
        av = result.get("allowance")
        if av is None:
            print("allowance: null")
        else:
            _print_amount("allowance", av)
    elif "allowances" in result and result["allowances"] is not None:
        _print_allowances("allowances", result["allowances"])
    elif "allowances" in result:
        print("allowances: null")
    else:
        print("allowance: (not returned by API for this call)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
