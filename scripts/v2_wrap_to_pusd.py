"""V2 wrap-to-pUSD bootstrap script.

For a Polymarket POLY_GNOSIS_SAFE wallet (signature_type=2) holding USDC.e,
batches and submits via the Polymarket Relayer:

  1. USDC.e.approve(CollateralOnramp, amount)
  2. CollateralOnramp.wrap(USDC.e, Safe, amount)        -> mints pUSD into Safe
  3. pUSD.approve(V2 Exchange,         MAX_UINT256)
  4. pUSD.approve(NegRisk Adapter,     MAX_UINT256)
  5. pUSD.approve(NegRisk Exchange v2, MAX_UINT256)

All five execute as one atomic Safe transaction through the Polymarket
Relayer (gas paid by Polymarket).

Run:
    python scripts/v2_wrap_to_pusd.py            # dry run, prints plan, exits
    python scripts/v2_wrap_to_pusd.py --yes      # actually submit on-chain

Required env (in your .env):
    POLYMARKET_PK
    POLYMARKET_FUNDER             (your Safe address)
    POLYMARKET_SIGNATURE_TYPE     (must be 2)

    POLY_BUILDER_API_KEY
    POLY_BUILDER_API_SECRET
    POLY_BUILDER_API_PASSPHRASE

Optional:
    POLYGON_RPC                   (default: public Polygon RPCs)
    POLYMARKET_RELAYER_URL        (default: https://relayer-v2.polymarket.com)
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal

import httpx
from eth_abi import encode
from eth_utils import keccak, to_checksum_address

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass


# -----------------------------------------------------------------------------
# Constants (Polygon mainnet, chain 137)
# -----------------------------------------------------------------------------

CHAIN_ID = 137

USDCe_ADDR = to_checksum_address("0x2791Bca1f2De4661ED88A30C99A7a9449Aa84174")
PUSD_ADDR  = to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")

COLLATERAL_ONRAMP   = to_checksum_address("0x93070a847efEf7F70739046A929D47a521F5B8ee")
V2_EXCHANGE         = to_checksum_address("0xE111180000d2663C0091e4f400237545B87B996B")
V2_NEGRISK_ADAPTER  = to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
V2_NEGRISK_EXCHANGE = to_checksum_address("0xe2222d279d744050d28e00520010520000310F59")

MAX_UINT256 = 2**256 - 1

POLYGON_RPC_DEFAULTS = (
    "https://polygon.llamarpc.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _eth_call(rpcs: list[str], to: str, data: str) -> str | None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
               "params": [{"to": to, "data": data}, "latest"]}
    last_err: str | None = None
    for rpc in rpcs:
        try:
            r = httpx.post(rpc, json=payload, timeout=15.0)
            r.raise_for_status()
            j = r.json()
        except Exception as exc:
            last_err = f"{rpc}: {exc}"
            continue
        if "error" in j:
            last_err = f"{rpc}: {j['error']}"
            continue
        return j.get("result")
    if last_err:
        print(f"  [rpc error, last: {last_err}]", file=sys.stderr)
    return None


def _erc20_balance_of(rpcs: list[str], token: str, owner: str) -> int:
    sel = "0x70a08231"  # balanceOf(address)
    pad = owner.lower().removeprefix("0x").rjust(64, "0")
    raw = _eth_call(rpcs, token, sel + pad)
    if not raw or raw == "0x":
        return 0
    return int(raw, 16)


def encode_approve(spender: str, amount: int) -> str:
    sel = keccak(text="approve(address,uint256)")[:4]
    body = encode(["address", "uint256"], [to_checksum_address(spender), amount])
    return "0x" + (sel + body).hex()


def encode_wrap(asset: str, to: str, amount: int) -> str:
    sel = keccak(text="wrap(address,address,uint256)")[:4]
    body = encode(["address", "address", "uint256"],
                  [to_checksum_address(asset), to_checksum_address(to), amount])
    return "0x" + (sel + body).hex()


def fmt6(raw: int) -> str:
    return f"{Decimal(raw) / Decimal(10**6):,.6f}"


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--yes", action="store_true",
        help="actually submit the transaction; without this flag we only "
             "print the plan and exit",
    )
    args = parser.parse_args()

    # -- env --
    pk = os.environ.get("POLYMARKET_PK")
    funder_env = os.environ.get("POLYMARKET_FUNDER")
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))

    builder_key = os.environ.get("POLY_BUILDER_API_KEY")
    builder_secret = os.environ.get("POLY_BUILDER_API_SECRET")
    builder_pass = os.environ.get("POLY_BUILDER_API_PASSPHRASE")

    relayer_url = os.environ.get("POLYMARKET_RELAYER_URL", "https://relayer-v2.polymarket.com")
    env_rpc = os.environ.get("POLYGON_RPC")
    rpcs = [env_rpc] if env_rpc else list(POLYGON_RPC_DEFAULTS)

    if not pk or not funder_env:
        print("ERROR: POLYMARKET_PK and POLYMARKET_FUNDER must be set", file=sys.stderr)
        return 2
    if sig_type != 2:
        print(f"ERROR: this script only supports signature_type=2 (POLY_GNOSIS_SAFE); "
              f"got {sig_type}", file=sys.stderr)
        return 2
    if not (builder_key and builder_secret and builder_pass):
        print("ERROR: POLY_BUILDER_API_KEY / POLY_BUILDER_API_SECRET / "
              "POLY_BUILDER_API_PASSPHRASE must all be set", file=sys.stderr)
        return 2

    funder = to_checksum_address(funder_env)

    # -- imports inline so missing optional deps fail loud only when run --
    from py_builder_relayer_client.client import RelayClient
    from py_builder_relayer_client.models import SafeTransaction, OperationType
    from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig

    builder_config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=builder_key,
            secret=builder_secret,
            passphrase=builder_pass,
        )
    )

    client = RelayClient(
        relayer_url=relayer_url,
        chain_id=CHAIN_ID,
        private_key=pk,
        builder_config=builder_config,
    )
    eoa = client.signer.address()
    expected_safe = client.get_expected_safe()

    print("=" * 78)
    print("CONTEXT")
    print("=" * 78)
    print(f"  EOA (signer)      : {eoa}")
    print(f"  POLYMARKET_FUNDER : {funder}")
    print(f"  Expected Safe     : {expected_safe}")
    print(f"  Relayer URL       : {relayer_url}")
    print(f"  Builder API key   : {builder_key[:8]}…")

    if expected_safe.lower() != funder.lower():
        print()
        print(f"ERROR: POLYMARKET_FUNDER ({funder}) does not match the Safe")
        print(f"       deterministically derived from your EOA ({expected_safe}).")
        return 2

    deployed = client.get_deployed(expected_safe)
    print(f"  Safe deployed?    : {deployed}")
    if not deployed:
        print()
        print("ERROR: Safe contract is not deployed on-chain. Make a small")
        print("       deposit via Polymarket UI to trigger deployment, then re-run.")
        return 2

    usdce_balance = _erc20_balance_of(rpcs, USDCe_ADDR, expected_safe)
    pusd_balance  = _erc20_balance_of(rpcs, PUSD_ADDR,  expected_safe)

    print()
    print(f"  USDC.e on Safe    : {fmt6(usdce_balance)}")
    print(f"  pUSD   on Safe    : {fmt6(pusd_balance)}")

    if usdce_balance == 0:
        print()
        print("ERROR: Safe holds 0 USDC.e — nothing to wrap. If your funds are")
        print("       elsewhere (e.g. native USDC, in your EOA, etc.), move them")
        print("       to the Safe first via the Polymarket UI.")
        return 2

    # -- build the batched plan --
    txs_plan = [
        ("USDC.e.approve(CollateralOnramp, amount)",
         SafeTransaction(
             to=USDCe_ADDR,
             operation=OperationType.Call,
             data=encode_approve(COLLATERAL_ONRAMP, usdce_balance),
             value="0",
         )),
        (f"CollateralOnramp.wrap(USDC.e, Safe, amount)  -> mints {fmt6(usdce_balance)} pUSD into Safe",
         SafeTransaction(
             to=COLLATERAL_ONRAMP,
             operation=OperationType.Call,
             data=encode_wrap(USDCe_ADDR, expected_safe, usdce_balance),
             value="0",
         )),
        ("pUSD.approve(V2 Exchange,         MAX_UINT256)",
         SafeTransaction(
             to=PUSD_ADDR,
             operation=OperationType.Call,
             data=encode_approve(V2_EXCHANGE, MAX_UINT256),
             value="0",
         )),
        ("pUSD.approve(NegRisk Adapter,     MAX_UINT256)",
         SafeTransaction(
             to=PUSD_ADDR,
             operation=OperationType.Call,
             data=encode_approve(V2_NEGRISK_ADAPTER, MAX_UINT256),
             value="0",
         )),
        ("pUSD.approve(NegRisk Exchange v2, MAX_UINT256)",
         SafeTransaction(
             to=PUSD_ADDR,
             operation=OperationType.Call,
             data=encode_approve(V2_NEGRISK_EXCHANGE, MAX_UINT256),
             value="0",
         )),
    ]

    print()
    print("=" * 78)
    print("PLAN  (one atomic Safe transaction with 5 inner calls)")
    print("=" * 78)
    for i, (label, tx) in enumerate(txs_plan, 1):
        print(f"  {i}. {label}")
        print(f"       to      : {tx.to}")
        print(f"       data    : {tx.data[:42]}...{tx.data[-8:]}  ({len(tx.data)//2 - 1} bytes)")

    if not args.yes:
        print()
        print("DRY RUN. No transaction submitted.")
        print("Re-run with --yes to actually execute.")
        return 0

    print()
    print("=" * 78)
    print("SUBMITTING (via py_builder_relayer_client.RelayClient.execute)")
    print("=" * 78)
    safe_txs = [tx for _, tx in txs_plan]

    try:
        resp = client.execute(
            transactions=safe_txs,
            metadata="Tyrex_PM: wrap USDC.e to pUSD and approve V2 exchanges",
        )
    except Exception as exc:
        print(f"  client.execute failed: {exc}", file=sys.stderr)
        return 1

    print(f"  transaction_id   : {resp.transaction_id}")
    print(f"  transaction_hash : {resp.transaction_hash or '(pending)'}")
    print()
    print("Polling for terminal state (STATE_CONFIRMED / STATE_MINED) ...")
    final = resp.wait()

    print()
    print("=" * 78)
    print("RESULT")
    print("=" * 78)
    if final is None:
        print("  Timed out or relayer reported failure.")
        print("  Try fetching the transaction directly:")
        print(f"    python -c \"from py_builder_relayer_client.client import RelayClient;"
              f" ...; print(client.get_transaction('{resp.transaction_id}'))\"")
        return 1

    state = final.get("state")
    txhash = final.get("transactionHash") or resp.transaction_hash
    print(f"  Final state : {state}")
    print(f"  Tx hash     : {txhash}")
    if txhash:
        print(f"  Polygonscan : https://polygonscan.com/tx/{txhash}")
    if state in ("STATE_CONFIRMED", "STATE_MINED"):
        print()
        print("Wrap + approvals confirmed on-chain.")
        print("Re-run scripts/v2_collateral_probe.py to verify pUSD balance and allowances.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
