"""Print Polymarket CLOB collateral balance using the same env rules as Tyrex.

Loads repo-root ``.env``, then builds ``py-clob-client-v2.ClobClient`` exactly like
``tyrex_pm.venue.polymarket.clob_env.try_create_clob_client()`` (TYREX_* overrides
POLYMARKET_*). Calls ``GET /balance-allowance`` for ``COLLATERAL``. Does **not**
submit orders.

Usage::

    pip install -U tyrex-pm[live] python-dotenv
    python scripts/show_clob_wallet_balance.py
    python scripts/show_clob_wallet_balance.py --refresh

Secrets from ``.env`` are never printed (only whether API creds are set).

"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Show CLOB collateral balance from current .env")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Call /balance-allowance/update before GET (same as Tyrex wallet refresh)",
    )
    parser.add_argument(
        "--raw-json",
        action="store_true",
        help="Print the raw GET response JSON after the summary",
    )
    args = parser.parse_args()

    _load_dotenv()

    from tyrex_pm.venue.polymarket.clob_env import (
        resolve_clob_host,
        resolve_positions_wallet_address,
        try_create_clob_client,
    )
    from tyrex_pm.venue.polymarket.clob_wallet_sync import _v2_balance_to_usd

    pk_set = bool(_env_first("TYREX_PRIVATE_KEY", "POLYMARKET_PK"))
    if not pk_set:
        print("ERROR: Set TYREX_PRIVATE_KEY or POLYMARKET_PK in .env", file=sys.stderr)
        return 2

    host = resolve_clob_host()
    chain_id = int(_env_first("TYREX_CHAIN_ID") or "137")
    sig_raw = (
        _env_first("TYREX_SIGNATURE_TYPE")
        or _env_first("POLYMARKET_SIGNATURE_TYPE")
        or "0"
    )
    sig_t = int(sig_raw)
    funder_raw = _env_first("TYREX_FUNDER", "POLYMARKET_FUNDER")
    api_triplet = all(
        (
            bool(_env_first("POLYMARKET_API_KEY")),
            bool(_env_first("POLYMARKET_API_SECRET")),
            bool(_env_first("POLYMARKET_PASSPHRASE")),
        )
    )

    client = try_create_clob_client()
    if client is None:
        print(
            "ERROR: Could not create CLOB client (missing deps or API credential derivation failed). "
            "Install tyrex-pm[live] and ensure POLYMARKET_API_* are set or derivation works.",
            file=sys.stderr,
        )
        return 3

    try:
        signer_addr = client.get_address()
    except Exception as e:
        print(f"ERROR: get_address failed: {e}", file=sys.stderr)
        return 3

    positions_wallet = resolve_positions_wallet_address(client)

    from py_clob_client_v2 import AssetType, BalanceAllowanceParams

    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)

    if args.refresh:
        upd = client.update_balance_allowance(params)
        if args.raw_json:
            print("UPDATE:", json.dumps(upd if isinstance(upd, dict) else {"raw": str(upd)}, indent=2))

    payload = client.get_balance_allowance(params)
    if not isinstance(payload, dict):
        print(f"Unexpected response type: {type(payload)}", file=sys.stderr)
        print(payload)
        return 4

    bal_usd, allow_usd = _v2_balance_to_usd(payload)

    print("CLOB collateral (matches Tyrex wallet_sync interpretation)")
    print(f"  host               : {host}")
    print(f"  chain_id           : {chain_id}")
    print(f"  signature_type     : {sig_t}")
    print(f"  signer_eoa         : {signer_addr}")
    print(f"  funder (maker env) : {funder_raw or '(none — EOA mode)'}")
    print(f"  positions_api_user : {positions_wallet or '(same as signer if unset)'}")
    print(f"  api_key_triplet    : {'set' if api_triplet else 'unset (derived)'}")
    print()
    if bal_usd is None:
        print("  balance_usd        : (unknown / missing in payload)")
    else:
        print(f"  balance_usd        : {bal_usd:.6f}")
    if allow_usd is None:
        print("  allowance_usd (min): (unknown / missing in payload)")
    else:
        print(f"  allowance_usd (min): {allow_usd:.6f}")

    allowances = payload.get("allowances")
    if isinstance(allowances, dict) and allowances:
        print()
        print("  per-exchange allowances (USD, min across these binds risk):")
        scale = Decimal(10) ** 6

        def d(x: object) -> Decimal:
            try:
                return Decimal(str(x))
            except Exception:
                return Decimal("0")

        for addr, raw in sorted(allowances.items()):
            print(f"    {addr} -> {d(raw) / scale:.6f}")

    if args.raw_json:
        print()
        print(json.dumps(payload, indent=2))

    eff = min(bal_usd or Decimal("0"), allow_usd or Decimal("0")) if bal_usd is not None and allow_usd is not None else None
    if eff is not None:
        print()
        print(f"  effective_for_buy ~ min(balance, allowance) = {eff:.6f} USD")

    return 0


if __name__ == "__main__":
    sys.exit(main())
