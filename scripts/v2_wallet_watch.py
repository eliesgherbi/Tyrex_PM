"""V2 wallet / positions console viewer (isolated from ``tyrex-pm run``).

Use this in a **second terminal** while you run live tests or the operator
wrap flow, until the Polymarket UI shows V2 orders and balances.

What it does:
  * **User WebSocket** — same channel as production
    (``wss://ws-subscriptions-clob.polymarket.com/ws/user``): live order
    placement / update / cancel events and trade notifications, merged into a
    :class:`~tyrex_pm.state.wallet_store.WalletStore` like the runtime.
  * **REST (periodic)** — V2 CLOB balance + open orders
    (:func:`~tyrex_pm.venue.polymarket.clob_wallet_sync.refresh_wallet_from_clob`)
    and data-api positions
    (:func:`~tyrex_pm.venue.polymarket.positions_sync.refresh_positions_from_data_api`).

Collateral shown is **Polymarket USD on the CLOB** (what risk gates use), not
your MetaMask pUSD balance; use ``scripts/v2_wallet_mode.py`` for on-chain
token balances if needed.

Environment (same as live; load ``.env`` if python-dotenv is installed):
  ``TYREX_PRIVATE_KEY`` / ``POLYMARKET_PK``, optional ``TYREX_FUNDER`` /
  ``POLYMARKET_FUNDER``, ``TYREX_SIGNATURE_TYPE``, ``TYREX_CLOB_HOST``, etc.

Optional:
  ``TYREX_USER_WS_DISABLE=1`` — REST-only snapshots (no websocket).
  ``TYREX_WATCH_INTERVAL_S`` — seconds between REST refreshes (default ``5``).
  ``TYREX_DATA_API_BASE`` — override data-api host.
  ``TYREX_USER_WS_URL`` — override user websocket URL.

Run:
    python scripts/v2_wallet_watch.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from decimal import Decimal

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    pass

from tyrex_pm.ingestion.user_stream import run_user_ws_ingest
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.venue.polymarket.clob_env import (
    resolve_positions_wallet_address,
    try_create_clob_client,
)
from tyrex_pm.venue.polymarket.clob_wallet_sync import refresh_wallet_from_clob
from tyrex_pm.venue.polymarket.data_api_client import DEFAULT_DATA_API_BASE, DataApiClient
from tyrex_pm.venue.polymarket.positions_sync import refresh_positions_from_data_api


def _short_token(s: str, head: int = 10, tail: int = 6) -> str:
    if len(s) <= head + tail + 3:
        return s
    return f"{s[:head]}…{s[-tail:]}"


def _print_snapshot(
    wallet: WalletStore,
    *,
    positions_wallet: str | None,
    ws_note: str,
) -> None:
    bal = wallet.usdc_balance
    allow = wallet.usdc_allowance
    bal_s = f"{bal:,.6f}" if bal is not None else "—"
    allow_s = f"{allow:,.6f}" if allow is not None else "—"

    notional = Decimal("0")
    for p in wallet.positions.values():
        if p.avg_price_usd is not None:
            notional += p.qty * p.avg_price_usd

    print()
    print("=" * 78)
    print(f"  CLOB collateral (Polymarket USD)  balance={bal_s}  min_allowance={allow_s}")
    print(f"  Positions wallet (data-api)       {positions_wallet or '— (set TYREX_FUNDER or EOA-only)'}")
    print(f"  User WS                           {ws_note}")
    if wallet.last_sync_ts:
        print(f"  last CLOB REST sync               {wallet.last_sync_ts.isoformat()}")
    if wallet.last_positions_sync_ts:
        print(f"  last positions REST sync          {wallet.last_positions_sync_ts.isoformat()}")
    print("-" * 78)
    print(f"  Open orders (merged REST+WS): {len(wallet.open_orders)}")
    for o in sorted(wallet.open_orders, key=lambda x: str(x.token_id)):
        tid = _short_token(str(o.token_id))
        oid = _short_token(str(o.venue_order_id), 12, 6) if o.venue_order_id else "—"
        print(
            f"    {o.side.value:4}  px={o.limit_price}  rem={o.remaining_size}  "
            f"token={tid}  vid={oid}"
        )
    print("-" * 78)
    npos = len(wallet.positions)
    print(
        f"  Positions: {npos}  |  Σ(qty×avg) USD (where avg known) = "
        f"{notional:,.6f}"
    )
    for tid, p in sorted(wallet.positions.items(), key=lambda x: str(x[0])):
        ap = f"{p.avg_price_usd}" if p.avg_price_usd is not None else "—"
        print(f"    {_short_token(str(tid))}  qty={p.qty}  avg={ap}")
    print("-" * 78)
    recent = wallet.trade_fill_records[-8:]
    if recent:
        print(f"  Recent user-WS trades (last {len(recent)} of {len(wallet.trade_fill_records)}):")
        for r in recent:
            print(
                f"    {r.ts_utc.isoformat()}  {r.side.value}  sz={r.size}  px={r.price}  "
                f"{r.status}  token={_short_token(str(r.token_id))}"
            )
    else:
        print("  Recent user-WS trades: (none yet — waiting for WS events)")
    print("=" * 78)


async def _rest_poll_loop(
    coord: RuntimeCoordinator,
    clob_client: object,
    data: DataApiClient,
    positions_wallet: str | None,
    interval_s: float,
    stop: asyncio.Event,
) -> None:
    warned_no_positions = False
    while not stop.is_set():
        try:
            await refresh_wallet_from_clob(coord.wallet, clob_client)
            if positions_wallet:
                await refresh_positions_from_data_api(
                    coord.wallet, data, positions_wallet
                )
            elif not warned_no_positions:
                print(
                    "[v2_wallet_watch] No positions wallet address; "
                    "only CLOB collateral + open orders will refresh. "
                    "Set TYREX_FUNDER / POLYMARKET_FUNDER if you use a proxy.",
                    file=sys.stderr,
                )
                warned_no_positions = True
        except Exception:
            logging.exception("REST snapshot failed")
        _print_snapshot(
            coord.wallet,
            positions_wallet=positions_wallet,
            ws_note="enabled (orders/trades apply to this viewer)"
            if os.environ.get("TYREX_USER_WS_DISABLE", "").strip() != "1"
            else "disabled (TYREX_USER_WS_DISABLE=1)",
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return
        except asyncio.TimeoutError:
            continue


async def _run_watch(interval_s: float) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    clob = try_create_clob_client()
    if clob is None:
        print(
            "Could not build V2 CLOB client (check TYREX_PRIVATE_KEY / POLYMARKET_PK).",
            file=sys.stderr,
        )
        raise SystemExit(2)

    creds = getattr(clob, "creds", None)
    if creds is None:
        print("CLOB client has no API credentials after derive.", file=sys.stderr)
        raise SystemExit(2)

    positions_wallet = resolve_positions_wallet_address(clob)
    data_base = os.environ.get("TYREX_DATA_API_BASE", DEFAULT_DATA_API_BASE).strip()
    data_client = DataApiClient(base_url=data_base)

    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore())
    stop = asyncio.Event()

    ws_off = os.environ.get("TYREX_USER_WS_DISABLE", "").strip() == "1"
    tasks: list[asyncio.Task[None]] = []
    if not ws_off:
        tasks.append(
            asyncio.create_task(
                run_user_ws_ingest(
                    coord,
                    api_key=creds.api_key,
                    secret=creds.api_secret,
                    passphrase=creds.api_passphrase,
                    stop=stop,
                )
            )
        )
    tasks.append(
        asyncio.create_task(
            _rest_poll_loop(
                coord,
                clob,
                data_client,
                positions_wallet,
                interval_s,
                stop,
            )
        )
    )

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        stop.set()
        raise
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> int:
    p = argparse.ArgumentParser(description="V2 wallet / orders / positions console viewer.")
    p.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("TYREX_WATCH_INTERVAL_S", "5")),
        help="Seconds between REST refreshes (default: env TYREX_WATCH_INTERVAL_S or 5)",
    )
    args = p.parse_args()
    if args.interval <= 0:
        print("--interval must be positive", file=sys.stderr)
        return 2

    try:
        asyncio.run(_run_watch(args.interval))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
