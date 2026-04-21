"""V2 collateral probe.

Diagnoses why the V2 venue reports zero collateral for the configured wallet.

What it reports:
  1. The V2 venue's *raw* response for GET /balance-allowance
     (no fallback masking, no tyrex_pm wallet store in the path).
  2. On-chain ERC-20 balances of likely USDC variants on
     both the EOA (signer) and the funder/proxy address.
  3. On-chain ERC-20 allowances from EOA and funder to the
     V2 exchange contracts (exchange_v2 and neg_risk_exchange_v2).

Run:
    python scripts/v2_collateral_probe.py

Required env vars (already in your .env):
    POLYMARKET_PK
    POLYMARKET_FUNDER
    POLYMARKET_SIGNATURE_TYPE   (default 1 = POLY_PROXY)
Optional:
    TYREX_CLOB_HOST             (default https://clob-v2.polymarket.com)
    POLYGON_RPC                 (default https://polygon-rpc.com)
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass


POLYGON_RPC_DEFAULTS = (
    "https://polygon-rpc.com",
    "https://polygon.llamarpc.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
    "https://polygon-bor-rpc.publicnode.com",
)

# Known token contracts on Polygon mainnet (chain 137).
# We probe several USDC variants because the V2 venue's notion of
# "COLLATERAL" is opaque from the SDK and we want to see which one
# (if any) the funder actually holds.
TOKENS: dict[str, str] = {
    "USDC.e (bridged, V1 collateral)": "0x2791Bca1f2De4661ED88A30C99A7a9449Aa84174",
    "USDC (native, Circle)":           "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
}

# V2 exchange contracts on Polygon mainnet, taken directly from
# py_clob_client_v2.config.get_contract_config(137).
V2_EXCHANGE         = "0xE111180000d2663C0091e4f400237545B87B996B"
V2_NEGRISK_EXCHANGE = "0xe2222d279d744050d28e00520010520000310F59"

# ERC-20 4-byte selectors.
SEL_BALANCE_OF = "0x70a08231"  # balanceOf(address)
SEL_ALLOWANCE  = "0xdd62ed3e"  # allowance(address,address)


def _pad_addr(addr: str) -> str:
    """Left-pad an address to a 32-byte ABI word (no 0x)."""
    clean = addr.lower().removeprefix("0x")
    if len(clean) != 40:
        raise ValueError(f"bad address: {addr!r}")
    return clean.rjust(64, "0")


def _eth_call(
    client: httpx.Client, rpcs: list[str], to: str, data: str
) -> str | None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    }
    last_err: str | None = None
    for rpc_url in rpcs:
        try:
            r = client.post(rpc_url, json=payload, timeout=15.0)
            r.raise_for_status()
            j = r.json()
        except Exception as exc:
            last_err = f"{rpc_url}: {exc}"
            continue
        if "error" in j:
            last_err = f"{rpc_url}: {j['error']}"
            continue
        res = j.get("result")
        if not res or res == "0x":
            return None
        return res
    if last_err:
        print(f"    [rpc error, all endpoints failed; last: {last_err}]")
    return None


def erc20_balance(c: httpx.Client, rpcs: list[str], token: str, owner: str) -> int | None:
    raw = _eth_call(c, rpcs, token, SEL_BALANCE_OF + _pad_addr(owner))
    return None if raw is None else int(raw, 16)


def erc20_allowance(
    c: httpx.Client, rpcs: list[str], token: str, owner: str, spender: str
) -> int | None:
    raw = _eth_call(
        c, rpcs, token, SEL_ALLOWANCE + _pad_addr(owner) + _pad_addr(spender)
    )
    return None if raw is None else int(raw, 16)


def fmt6(x: int | None) -> str:
    """Format a 6-decimal token integer for display."""
    if x is None:
        return "n/a"
    return f"{Decimal(x) / Decimal(10**6):,.6f}"


def hr(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    pk = os.environ.get("POLYMARKET_PK")
    funder = os.environ.get("POLYMARKET_FUNDER")
    if not pk or not funder:
        print(
            "ERROR: POLYMARKET_PK and POLYMARKET_FUNDER must be set in env",
            file=sys.stderr,
        )
        return 2

    sig_t = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1"))
    host = os.environ.get("TYREX_CLOB_HOST", "https://clob-v2.polymarket.com")
    env_rpc = os.environ.get("POLYGON_RPC")
    rpcs = [env_rpc] if env_rpc else list(POLYGON_RPC_DEFAULTS)

    try:
        from py_clob_client_v2 import (  # type: ignore[import-not-found]
            AssetType,
            BalanceAllowanceParams,
            ClobClient,
        )
    except ImportError as exc:
        print(f"ERROR: py-clob-client-v2 is required: {exc}", file=sys.stderr)
        return 2

    client = ClobClient(
        host,
        chain_id=137,
        key=pk,
        signature_type=sig_t,
        funder=funder,
    )
    eoa = client.get_address()

    creds = client.create_or_derive_api_key()
    client.set_api_creds(creds)

    hr("ADDRESSES")
    print(f"  EOA (signer):     {eoa}")
    print(f"  Funder (proxy):   {funder}")
    print(f"  signature_type:   {sig_t}  (1 = POLY_PROXY)")
    print(f"  V2 host:          {host}")
    print(f"  Polygon RPCs:     {', '.join(rpcs)}")

    hr("1. V2 VENUE: GET /balance-allowance?asset_type=COLLATERAL  (raw)")
    try:
        raw = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        print(json.dumps(raw, indent=2, default=str))
    except Exception as exc:
        print(f"  ERROR: {exc}")

    with httpx.Client() as h:
        hr("2. ON-CHAIN ERC-20 BALANCES (Polygon mainnet)")
        for label, token in TOKENS.items():
            print(f"\n  {label}")
            print(f"    contract: {token}")
            for who, addr in (("EOA          ", eoa), ("Funder/proxy ", funder)):
                bal = erc20_balance(h, rpcs, token, addr)
                print(f"    {who} ({addr}):  balance = {fmt6(bal)}")

        hr("3. ON-CHAIN ERC-20 ALLOWANCES TO V2 EXCHANGE CONTRACTS")
        for label, token in TOKENS.items():
            print(f"\n  {label}")
            print(f"    contract: {token}")
            for spender_label, spender in (
                ("V2 exchange         ", V2_EXCHANGE),
                ("V2 neg-risk exchange", V2_NEGRISK_EXCHANGE),
            ):
                for who, addr in (("EOA         ", eoa), ("Funder/proxy", funder)):
                    a = erc20_allowance(h, rpcs, token, addr, spender)
                    print(
                        f"    {who} -> {spender_label}: allowance = {fmt6(a)}"
                    )

    hr("INTERPRETATION GUIDE")
    print("  - Section 1 is the source of truth for what the venue believes.")
    print("    If 'balance' is 0 there, the venue will reject your order with")
    print("    'not enough balance / allowance' even if Section 2 shows tokens.")
    print()
    print("  - Tokens in EOA, 0 in Funder/proxy:")
    print("      Funds are in the wrong address. Deposit via Polymarket UI so")
    print("      the proxy receives them, or move them on-chain to the proxy.")
    print()
    print("  - Tokens in Funder/proxy, but Section 1 still 0:")
    print("      The token you hold is not what V2 counts as COLLATERAL.")
    print("      V2 uses Polymarket's own collateral; convert/wrap via the")
    print("      Polymarket UI's Deposit flow.")
    print()
    print("  - Section 1 'balance' OK, but Section 3 allowance = 0 for the")
    print("    funder against V2 exchange:")
    print("      Approve the V2 exchange contract to spend your collateral.")
    print("      The Polymarket UI normally sets this on first V2 deposit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
