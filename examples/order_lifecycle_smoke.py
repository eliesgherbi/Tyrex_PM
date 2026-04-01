#!/usr/bin/env python3
"""
Milestone v1.02: supervised LIMIT order lifecycle smoke (place → acknowledge → cancel).

Uses py-clob-client directly (same L2 env pattern as scripts/verify_polymarket_auth.py).
This is intentionally minimal vs a full Nautilus TradingNode path; see the v1.02 runbook.

Safety:
  - Default mode is *dry-run* (prints plan only).
  - Live execution requires --execute plus env
    TYREX_ORDER_SMOKE_CONFIRM=I_UNDERSTAND.
  - BUY: optional TYREX_SMOKE_MIN_BUY_NOTIONAL_USD (default 1) avoids sub-$1 notional
    rejects; set 0 to disable client-side bump.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv_files() -> list[Path]:
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("ERROR: python-dotenv not installed. Run: pip install -e .", file=sys.stderr)
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


def _build_clob_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    pk = os.environ.get("POLYMARKET_PK")
    if not pk:
        print("ERROR: POLYMARKET_PK not set.", file=sys.stderr)
        sys.exit(1)

    host = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
    chain_id = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
    funder = os.environ.get("POLYMARKET_FUNDER")

    if sig_type in (1, 2) and not funder:
        print("ERROR: POLYMARKET_FUNDER required for signature_type 1 or 2.", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("POLYMARKET_API_KEY")
    api_secret = os.environ.get("POLYMARKET_API_SECRET")
    passphrase = os.environ.get("POLYMARKET_PASSPHRASE")

    if (api_key or api_secret or passphrase) and not (api_key and api_secret and passphrase):
        print("ERROR: incomplete L2 trio in env.", file=sys.stderr)
        sys.exit(1)

    if api_key and api_secret and passphrase:
        creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=passphrase)
    else:
        temp = ClobClient(host, key=pk, chain_id=chain_id)
        creds = temp.create_or_derive_api_creds()

    kwargs: dict = {"host": host, "key": pk, "chain_id": chain_id, "creds": creds}
    if funder:
        kwargs["signature_type"] = sig_type
        kwargs["funder"] = funder
    else:
        kwargs["signature_type"] = 0

    return ClobClient(**kwargs)


def _extract_order_id(response: object) -> str:
    if not isinstance(response, dict):
        raise ValueError(f"Unexpected post response type: {type(response).__name__}")
    for key in ("orderID", "orderId", "order_id", "id"):
        val = response.get(key)
        if val is not None:
            return str(val)
    raise ValueError(f"Could not find order id in response keys: {sorted(response.keys())}")


def _safe_float(x: str | None, default: float) -> float:
    if x is None:
        return default
    try:
        return float(x)
    except ValueError:
        return default


def _min_buy_notional_floor_usd() -> float:
    """CLOB can reject low-$ BUYs; override with TYREX_SMOKE_MIN_BUY_NOTIONAL_USD (0 disables)."""
    raw = os.environ.get("TYREX_SMOKE_MIN_BUY_NOTIONAL_USD", "1")
    try:
        v = float(raw)
    except ValueError:
        v = 1.0
    return max(0.0, v)


def _suggest_limit_price(side: str, tick: float) -> float:
    """Pick a defensive LIMIT well away from typical mid (best-effort smoke default)."""
    hi = 1.0 - tick
    lo = tick
    if side.upper() == "BUY":
        p = min(0.02, hi)
        out = max(lo, p)
    else:
        p = max(0.98, lo)
        out = min(hi, p)
    return round(out / tick) * tick


def main() -> int:
    # Load `.env` before argparse reads os.environ (so TYREX_SMOKE_* from .env apply).
    loaded = _load_dotenv_files()
    if loaded:
        print("Config: loaded .env:", ", ".join(str(p) for p in loaded))
    else:
        print(f"Config: no .env at {REPO_ROOT / '.env'} (process env only).")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--token-id",
        default=os.environ.get("TYREX_SMOKE_TOKEN_ID"),
        help="CLOB token id",
    )
    parser.add_argument(
        "--side",
        default=os.environ.get("TYREX_SMOKE_SIDE", "BUY"),
        choices=("BUY", "SELL"),
    )
    parser.add_argument(
        "--size",
        type=float,
        default=None,
        help="Shares (defaults from book min or TYREX_SMOKE_SIZE)",
    )
    parser.add_argument(
        "--price",
        type=float,
        default=None,
        help="Override LIMIT price (else TYREX_SMOKE_PRICE or book-derived default)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Submit and cancel a real order (requires TYREX_ORDER_SMOKE_CONFIRM=I_UNDERSTAND).",
    )
    args = parser.parse_args()

    if not args.token_id:
        print("ERROR: token id required (--token-id or TYREX_SMOKE_TOKEN_ID).", file=sys.stderr)
        return 1

    if args.execute and os.environ.get("TYREX_ORDER_SMOKE_CONFIRM") != "I_UNDERSTAND":
        print(
            "ERROR: refusing --execute without TYREX_ORDER_SMOKE_CONFIRM=I_UNDERSTAND "
            "(supervised gate).",
            file=sys.stderr,
        )
        return 1

    client = _build_clob_client()
    book = client.get_order_book(args.token_id)
    tick = float(str(client.get_tick_size(args.token_id)))
    min_size = _safe_float(book.min_order_size, 0.0)
    env_size = os.environ.get("TYREX_SMOKE_SIZE")
    size = args.size
    if size is None:
        size = float(env_size) if env_size else max(min_size, 5.0) if min_size else 5.0
    size_requested = size
    share_clamped = False
    if min_size > 0 and size < min_size:
        print(
            f"NOTE: size {size} is below venue min_order_size {min_size}; "
            f"using {min_size} for this run.",
        )
        size = min_size
        share_clamped = True
    price = args.price
    if price is None:
        env_price = os.environ.get("TYREX_SMOKE_PRICE")
        if env_price:
            try:
                price = float(env_price)
            except ValueError:
                price = None
    if price is None:
        price = _suggest_limit_price(args.side, tick)

    min_buy_notional = 0.0
    buy_notional_bump = False
    if args.side.upper() == "BUY":
        min_buy_notional = _min_buy_notional_floor_usd()
        if min_buy_notional > 0 and price > 0:
            est = price * size
            if est + 1e-9 < min_buy_notional:
                need_shares = math.ceil((min_buy_notional / price) - 1e-12)
                new_sz = max(size, need_shares)
                if min_size > 0:
                    new_sz = max(new_sz, min_size)
                if new_sz > size:
                    print(
                        f"NOTE: BUY estimated notional ${est:.2f} is below assumed "
                        f"venue minimum ${min_buy_notional:.2f}; "
                        f"raising size {size} -> {new_sz}.",
                    )
                    size = float(new_sz)
                    buy_notional_bump = True

    plan = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "token_id": args.token_id[:12] + "…" if len(args.token_id) > 14 else args.token_id,
        "side": args.side,
        "size": size,
        "price": price,
        "tick": tick,
        "min_order_size": min_size or None,
        "execute": args.execute,
    }
    if size_requested != size:
        plan["size_requested"] = size_requested
    if share_clamped:
        plan["size_adjusted_to_min_order_size"] = True
    if buy_notional_bump:
        plan["size_adjusted_for_min_buy_notional"] = True
    if args.side.upper() == "BUY" and min_buy_notional > 0:
        plan["estimated_buy_notional_usd"] = round(float(price) * float(size), 6)
        plan["min_buy_notional_usd_assumption"] = min_buy_notional
    print("Plan:", json.dumps(plan, indent=2))

    if not args.execute:
        print("Dry-run only. Re-run with --execute after completing the runbook checklist.")
        return 0

    from py_clob_client.clob_types import OrderArgs

    fee_bps = client.get_fee_rate_bps(args.token_id)
    order_args = OrderArgs(
        token_id=args.token_id,
        price=float(price),
        size=float(size),
        side=args.side,
        fee_rate_bps=fee_bps,
    )

    t0 = time.perf_counter()
    print(f"Submitting LIMIT {args.side} @ {price} size={size} …")
    post_resp = client.create_and_post_order(order_args)
    order_id = _extract_order_id(post_resp)
    dt_ms = (time.perf_counter() - t0) * 1000
    print(f"ACK: order_id={order_id} (round_trip_ms≈{dt_ms:.1f})")

    print("Canceling …")
    t1 = time.perf_counter()
    cancel_resp = client.cancel(order_id)
    dt2_ms = (time.perf_counter() - t1) * 1000
    print(f"Cancel response ({dt2_ms:.1f} ms):", json.dumps(cancel_resp)[:500])
    print("Done: supervised smoke completed (verify terminal state in UI if required).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
