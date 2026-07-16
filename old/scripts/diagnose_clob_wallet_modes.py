"""Diagnose which V2 wallet mode/funder has tradable Polymarket collateral.

This is deliberately evidence-first:

1. Load `.env`.
2. Derive the signer EOA from `POLYMARKET_PK`.
3. Build candidate funder addresses from:
   - `POLYMARKET_FUNDER` / `TYREX_FUNDER`
   - the deterministic legacy Polymarket Safe derived from the EOA, when the
     builder relayer SDK is installed
   - any extra addresses passed on the command line
4. Try each candidate against the venue's `GET /balance-allowance` for each
   signature type in the probe set (see below).
5. Print on-chain pUSD/USDC balances for the same addresses.

**Default signature types:** if `POLYMARKET_SIGNATURE_TYPE` or `TYREX_SIGNATURE_TYPE`
is set in the environment, **only that integer** is probed (matches Gmail/Safe
``2`` workflows). If unset, probes **2** (GNOSIS_SAFE) and **3** (POLY_1271).

Use `--all-signature-types` to always probe both 2 and 3.

Run:
    python scripts/diagnose_clob_wallet_modes.py
    python scripts/diagnose_clob_wallet_modes.py 0x<address_from_polymarket_ui>
    python scripts/diagnose_clob_wallet_modes.py --all-signature-types

No secrets are printed.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

POLYGON_RPC_DEFAULTS = (
    "https://polygon-rpc.com",
    "https://polygon.llamarpc.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
    "https://polygon-bor-rpc.publicnode.com",
)

USDC_E = "0x2791Bca1f2De4661ED88A30C99A7a9449Aa84174"
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
SEL_BALANCE_OF = "0x70a08231"


@dataclass(frozen=True)
class Candidate:
    address: str
    source: str


def _load_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip('"').strip("'")
        return
    load_dotenv(env_path, override=True)


def _env_first(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _short(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if len(addr) > 12 else addr


def _pad_addr(addr: str) -> str:
    clean = addr.lower().removeprefix("0x")
    if len(clean) != 40:
        raise ValueError(f"bad address: {addr!r}")
    return clean.rjust(64, "0")


def _eth_call(client: httpx.Client, rpcs: list[str], to: str, data: str) -> str | None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    }
    for rpc_url in rpcs:
        try:
            r = client.post(rpc_url, json=payload, timeout=15.0)
            r.raise_for_status()
            j = r.json()
        except Exception:
            continue
        if "error" in j:
            continue
        res = j.get("result")
        if not res or res == "0x":
            return None
        return res
    return None


def _erc20_balance(client: httpx.Client, rpcs: list[str], token: str, owner: str) -> Decimal:
    raw = _eth_call(client, rpcs, token, SEL_BALANCE_OF + _pad_addr(owner))
    if raw is None:
        return Decimal("0")
    return Decimal(int(raw, 16)) / Decimal(10**6)


def _fmt6(x: Decimal | None) -> str:
    if x is None:
        return "n/a"
    return f"{x:,.6f}"


def _dec6_from_raw(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw)) / Decimal(10**6)
    except Exception:
        return None


def _venue_balance_allowance(payload: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    balance = _dec6_from_raw(payload.get("balance"))
    allowances = payload.get("allowances")
    if isinstance(allowances, dict) and allowances:
        vals = [_dec6_from_raw(v) for v in allowances.values()]
        vals = [v for v in vals if v is not None]
        allowance = min(vals) if vals else None
    else:
        allowance = _dec6_from_raw(payload.get("allowance") or payload.get("allowance_balance"))
    return balance, allowance


def _derive_signer_address(pk: str) -> str:
    from py_clob_client_v2 import ClobClient
    from tyrex_pm.venue.polymarket.clob_env import resolve_clob_host

    client = ClobClient(resolve_clob_host(), chain_id=137, key=pk, signature_type=0)
    return str(client.get_address())


def _derive_legacy_safe(eoa: str) -> Candidate | None:
    try:
        from py_builder_relayer_client.builder.derive import derive
        from py_builder_relayer_client.config import get_contract_config
    except ImportError:
        return None
    cfg = get_contract_config(137)
    return Candidate(address=derive(eoa, cfg.safe_factory), source="derived legacy Safe")


def _env_api_creds() -> Any | None:
    from tyrex_pm.venue.polymarket.clob_env import _resolve_env_api_creds

    return _resolve_env_api_creds()


def _candidate_addresses(eoa: str, extras: list[str]) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()

    def add(addr: str, source: str) -> None:
        addr = addr.strip()
        if not addr:
            return
        key = addr.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(Candidate(address=addr, source=source))

    add(_env_first("POLYMARKET_FUNDER", "TYREX_FUNDER"), "env POLYMARKET_FUNDER/TYREX_FUNDER")
    safe = _derive_legacy_safe(eoa)
    if safe is not None:
        add(safe.address, safe.source)
    for addr in extras:
        add(addr, "cli extra address")
    return out


def _print_header(title: str) -> None:
    print()
    print("=" * 88)
    print(title)
    print("=" * 88)


def _probe_venue_modes(pk: str, candidates: list[Candidate], signature_types: list[int]) -> None:
    from py_clob_client_v2 import AssetType, BalanceAllowanceParams, ClobClient
    from tyrex_pm.venue.polymarket.clob_env import _derive_or_create_api_key, resolve_clob_host

    host = resolve_clob_host()
    creds = _env_api_creds()
    _print_header("VENUE TRUTH: /balance-allowance by signature_type + funder")
    print(f"Host: {host}")
    print("Using direct POLYMARKET_API_* creds:", "yes" if creds is not None else "no")
    print()
    print("sig  funder(source)                                      balance      allowance    result")
    print("---  --------------------------------------------------  -----------  -----------  ------")
    for sig_t in signature_types:
        for cand in candidates:
            try:
                client = ClobClient(
                    host,
                    chain_id=137,
                    key=pk,
                    signature_type=sig_t,
                    funder=cand.address,
                )
                if creds is not None:
                    client.set_api_creds(creds)
                else:
                    d = _derive_or_create_api_key(client)
                    if d is None:
                        raise RuntimeError("could not derive/create CLOB API credentials")
                    client.set_api_creds(d)
                raw = client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                )
                if not isinstance(raw, dict):
                    raise RuntimeError(f"unexpected response shape: {type(raw).__name__}")
                bal, allow = _venue_balance_allowance(raw)
                result = "FUNDED" if (bal or Decimal("0")) > 0 and (allow or Decimal("0")) > 0 else "zero"
                print(
                    f"{sig_t:<3}  {_short(cand.address)} ({cand.source[:28]:28s})  "
                    f"{_fmt6(bal):>11}  {_fmt6(allow):>11}  {result}"
                )
            except Exception as exc:
                print(
                    f"{sig_t:<3}  {_short(cand.address)} ({cand.source[:28]:28s})  "
                    f"{'n/a':>11}  {'n/a':>11}  ERROR: {exc}"
                )


def _probe_onchain(candidates: list[Candidate], eoa: str) -> None:
    env_rpc = os.environ.get("POLYGON_RPC")
    rpcs = [env_rpc] if env_rpc else list(POLYGON_RPC_DEFAULTS)
    _print_header("ON-CHAIN BALANCES: where tokens physically sit on Polygon")
    print("address(source)                                      pUSD         USDC.e       native USDC")
    print("--------------------------------------------------  -----------  -----------  -----------")
    rows = [Candidate(eoa, "signer EOA"), *candidates]
    with httpx.Client() as h:
        for cand in rows:
            try:
                pusd = _erc20_balance(h, rpcs, PUSD, cand.address)
                usdce = _erc20_balance(h, rpcs, USDC_E, cand.address)
                usdc = _erc20_balance(h, rpcs, USDC_NATIVE, cand.address)
                print(
                    f"{_short(cand.address)} ({cand.source[:28]:28s})  "
                    f"{_fmt6(pusd):>11}  {_fmt6(usdce):>11}  {_fmt6(usdc):>11}"
                )
            except Exception as exc:
                print(f"{_short(cand.address)} ({cand.source[:28]:28s})  ERROR: {exc}")


def main() -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(
        description="CLOB balance-allowance + on-chain pUSD probe across funders.",
    )
    parser.add_argument(
        "--all-signature-types",
        action="store_true",
        help="Probe GNOSIS_SAFE (2) and POLY_1271 (3). Default: only env "
        "POLYMARKET_SIGNATURE_TYPE / TYREX_SIGNATURE_TYPE if set, else 2 and 3.",
    )
    parser.add_argument(
        "extra_addresses",
        nargs="*",
        metavar="ADDR",
        help="Additional candidate funder addresses from the Polymarket UI",
    )
    args = parser.parse_args()

    pk = _env_first("POLYMARKET_PK", "TYREX_PRIVATE_KEY")
    if not pk:
        print("ERROR: POLYMARKET_PK or TYREX_PRIVATE_KEY is required in .env", file=sys.stderr)
        return 2
    try:
        eoa = _derive_signer_address(pk)
        candidates = _candidate_addresses(eoa, args.extra_addresses)
    except Exception as exc:
        print(f"ERROR: could not prepare wallet-mode diagnosis: {exc}", file=sys.stderr)
        return 2
    if not candidates:
        print(
            "ERROR: no candidate funder addresses. Set POLYMARKET_FUNDER in .env "
            "or pass an address from the Polymarket UI as an argument.",
            file=sys.stderr,
        )
        return 2

    if args.all_signature_types:
        signature_types = [2, 3]
    else:
        sig_raw = _env_first("TYREX_SIGNATURE_TYPE", "POLYMARKET_SIGNATURE_TYPE").strip()
        if sig_raw:
            try:
                signature_types = [int(sig_raw)]
            except ValueError:
                print(f"ERROR: invalid POLYMARKET_SIGNATURE_TYPE: {sig_raw!r}", file=sys.stderr)
                return 2
        else:
            signature_types = [2, 3]

    _print_header("SIGNER + CANDIDATE FUNDERS")
    print(f"Signer EOA derived from private key: {eoa}")
    print(f"Signature types probed: {signature_types}")
    for cand in candidates:
        print(f"Candidate funder: {cand.address}  ({cand.source})")

    _probe_venue_modes(pk, candidates, signature_types)
    _probe_onchain(candidates, eoa)

    _print_header("HOW TO READ THIS")
    print("Use the row where VENUE TRUTH shows balance > 0 and allowance > 0.")
    print("That row's signature_type and funder are the values Tyrex should use.")
    print("If on-chain pUSD is > 0 but venue balance is 0, the address is not the")
    print("wallet mode/funder the CLOB recognizes for trading, or allowance is missing.")
    print("If all venue rows are zero but the UI shows cash, pass the UI wallet")
    print("address explicitly, or run with --all-signature-types to compare types 2 vs 3.")
    print("Gmail/Safe accounts: keep POLYMARKET_SIGNATURE_TYPE=2; use probe output for type 2 only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
