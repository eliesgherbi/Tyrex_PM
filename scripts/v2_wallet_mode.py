"""V2 wallet-mode detector.

Given your MetaMask EOA address (and optionally what Polymarket's
UI shows as the "deposit / wallet" address), this script:

  1. Derives the deterministic Polymarket SAFE address for that EOA.
  2. Checks if a contract is deployed at that Safe address.
  3. Reads the on-chain pUSD and USDC.e balances at both the EOA and
     the Safe so you can see where (if anywhere) your deposit landed.
  4. Optionally compares against an address you pass as the second arg
     (the address Polymarket showed you).
  5. Prints the exact .env values to use.

Run:
    python scripts/v2_wallet_mode.py <eoa_address>
    python scripts/v2_wallet_mode.py <eoa_address> <ui_funder_address>
"""

from __future__ import annotations

import sys
from decimal import Decimal

import httpx


RPCS = (
    "https://polygon.llamarpc.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
    "https://polygon-bor-rpc.publicnode.com",
)

# Token contracts on Polygon mainnet (chain 137).
USDCe = "0x2791Bca1f2De4661ED88A30C99A7a9449Aa84174"
USDCn = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # native Circle USDC
# pUSD (Polymarket USD) on Polygon — this is the V2 collateral token. Same hex
# value as MKR on Ethereum, but a different contract on Polygon. Source:
# py_clob_client_v2.config.get_contract_config(137).collateral and the
# Polymarket gasless docs which use this as `pUSD`.
pUSD  = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

# ERC-20 selector
SEL_BALANCE_OF = "0x70a08231"


def call(method: str, params: list) -> str | None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for rpc in RPCS:
        try:
            r = httpx.post(rpc, json=payload, timeout=15.0)
            r.raise_for_status()
            j = r.json()
        except Exception:
            continue
        if "error" in j:
            continue
        return j.get("result")
    return None


def pad_addr(addr: str) -> str:
    return addr.lower().removeprefix("0x").rjust(64, "0")


def fmt6(raw: str | None) -> str:
    if not raw or raw == "0x":
        return "0.000000"
    n = int(raw, 16)
    return f"{Decimal(n) / Decimal(10**6):,.6f}"


def erc20_balance(token: str, owner: str) -> str:
    return fmt6(call("eth_call", [{"to": token, "data": SEL_BALANCE_OF + pad_addr(owner)}, "latest"]))


def has_code(addr: str) -> bool:
    code = call("eth_getCode", [addr, "latest"])
    return bool(code and code != "0x")


def derive_safe(eoa: str) -> str:
    from py_builder_relayer_client.config import get_contract_config
    from py_builder_relayer_client.builder.derive import derive
    cfg = get_contract_config(137)
    return derive(eoa, cfg.safe_factory)


def balances_for(addr: str) -> dict[str, str]:
    return {
        "USDC.e": erc20_balance(USDCe, addr),
        "USDC  ": erc20_balance(USDCn, addr),
        "pUSD  ": erc20_balance(pUSD, addr),
    }


def print_balances(label: str, addr: str) -> None:
    bals = balances_for(addr)
    print(f"  {label} ({addr})")
    for tok, val in bals.items():
        print(f"    {tok}: {val}")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/v2_wallet_mode.py <eoa_address> [<ui_funder_address>]")
        return 2

    eoa = sys.argv[1].strip()
    ui_funder = sys.argv[2].strip() if len(sys.argv) >= 3 else None

    expected_safe = derive_safe(eoa)

    print("=" * 78)
    print("ADDRESSES")
    print("=" * 78)
    print(f"  MetaMask EOA            : {eoa}")
    print(f"  Expected Polymarket SAFE: {expected_safe}")
    if ui_funder:
        print(f"  Address from UI         : {ui_funder}")
        match = ui_funder.lower() == expected_safe.lower()
        print(f"  UI matches expected Safe: {'YES' if match else 'NO  <-- mismatch'}")
        if ui_funder.lower() == eoa.lower():
            print("  UI address == EOA       : EOA mode (signature_type=0)")

    print()
    print("=" * 78)
    print("ON-CHAIN STATUS")
    print("=" * 78)
    eoa_has_code = has_code(eoa)
    safe_has_code = has_code(expected_safe)
    print(f"  EOA  has contract code  : {eoa_has_code}  (expected: False)")
    print(f"  Safe has contract code  : {safe_has_code}")
    if not safe_has_code:
        print("    -> Safe is NOT deployed yet. This is normal before the first")
        print("       on-chain action. Polymarket auto-deploys it on first deposit.")

    if ui_funder and ui_funder.lower() != eoa.lower() and ui_funder.lower() != expected_safe.lower():
        ui_code = has_code(ui_funder)
        print(f"  UI funder has code      : {ui_code}")

    print()
    print("=" * 78)
    print("TOKEN BALANCES (USDC.e, native USDC, pUSD)")
    print("=" * 78)
    print_balances("EOA      ", eoa)
    print()
    print_balances("Safe     ", expected_safe)
    if ui_funder and ui_funder.lower() not in (eoa.lower(), expected_safe.lower()):
        print()
        print_balances("UI funder", ui_funder)

    # Aggregate "did any money land anywhere"
    all_targets = {eoa, expected_safe}
    if ui_funder:
        all_targets.add(ui_funder)
    any_money = False
    safe_pusd = erc20_balance(pUSD, expected_safe) != "0.000000"
    safe_usdce = erc20_balance(USDCe, expected_safe) != "0.000000"
    for a in all_targets:
        bals = balances_for(a)
        if any(v != "0.000000" for v in bals.values()):
            any_money = True
            break

    print()
    print("=" * 78)
    print("INTERPRETATION")
    print("=" * 78)
    if not any_money:
        print("  No USDC.e / USDC / pUSD found at any of the addresses checked.")
        print()
        print("  Likely reasons:")
        print("    1. Your deposit hasn't bridged yet (cross-chain bridges can")
        print("       take 1-15 minutes; check MetaMask Activity tab for the")
        print("       'to' address you actually sent funds to).")
        print("    2. The 'UI funder' you pasted is a temporary bridge intake")
        print("       address (Squid/LiFi/Across/etc.), not your final Polymarket")
        print("       wallet. The bridge forwards the funds to your Safe later.")
        print("    3. The deposit went to a wrong address entirely.")
        print()
        print("  ACTION: open MetaMask -> Activity, find the deposit, click it,")
        print("  copy the 'To:' address. Re-run this script with that address as")
        print("  the second arg, AND wait 5 more minutes if the tx was recent.")
    elif safe_pusd or safe_usdce:
        print("  Your Safe has funds! Mode = POLY_GNOSIS_SAFE (signature_type=2).")
        if not safe_has_code:
            print("  Note: Safe contract is not deployed yet. Polymarket will deploy")
            print("  it on the first action (your first trade or first relayer call).")
    else:
        print("  Funds found, but not on the expected Safe address. Inspect the")
        print("  balances above and figure out which address Polymarket actually")
        print("  considers your wallet. If it's the UI funder, that may be a")
        print("  different proxy type — share the output and we'll diagnose.")

    print()
    print("=" * 78)
    print(".env VALUES TO USE  (assuming SAFE mode)")
    print("=" * 78)
    print(f"  POLYMARKET_SIGNATURE_TYPE=2")
    print(f"  POLYMARKET_FUNDER={expected_safe}")
    print(f"  POLYMARKET_PK=0x<your-MetaMask-private-key>")
    print()
    print("If Polymarket's UI shows a *different* address than the expected Safe")
    print("above, paste that address here as the second arg and re-run; this")
    print("script will tell you whether it has code, balances, etc.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
