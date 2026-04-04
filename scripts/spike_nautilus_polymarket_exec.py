#!/usr/bin/env python3
"""
Step 2 / Path A spike: Nautilus ``TradingNode`` with **Polymarket data + execution**
clients (official live pattern), shared instrument loading, framework limit submit.

**Docs-confirmed / Examples-confirmed:** live Polymarket nodes pair ``PolymarketDataClient``
and ``PolymarketExecutionClient`` so instruments reach ``Cache`` via the data client
``_send_all_instruments_to_data_engine`` path (see Nautilus Polymarket integration docs
and ``examples/live/polymarket/polymarket_exec_tester.py``).

This script is **experimental** — not the production guru-follow path.

Submit path: Strategy.submit_order -> RiskEngine -> ExecEngine -> PolymarketExecutionClient
(``execution.py`` ``_submit_limit_order`` / ``_post_signed_order``).
**This file must not** call ``ClobClient.create_and_post_order`` for the spike order.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _ensure_polymarket_l2_env_for_nautilus_factory() -> None:
    """
    Nautilus ``get_polymarket_http_client`` (package-source) **requires**
    ``POLYMARKET_API_KEY`` / ``SECRET`` / ``PASSPHRASE`` via ``get_env_key`` — it
    does not derive them from PK like Tyrex ``clob_factory.py``.

    For Path A testing with PK-only ``.env``, derive L2 creds with the same
    py-clob pattern as ``verify_polymarket_auth`` and inject into **os.environ**
    before ``node.build()``. This is **not** the order submit path.

    **Package-source-confirmed:** ``adapters/polymarket/factories.py`` lines 82–86.
    """
    if os.environ.get("POLYMARKET_API_KEY") and os.environ.get("POLYMARKET_API_SECRET"):
        if os.environ.get("POLYMARKET_PASSPHRASE"):
            return
    pk = os.environ.get("POLYMARKET_PK")
    if not pk:
        return
    from py_clob_client.client import ClobClient

    host = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
    chain_id = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))
    temp = ClobClient(host, key=pk, chain_id=chain_id)
    creds = temp.create_or_derive_api_creds()
    os.environ.setdefault("POLYMARKET_API_KEY", creds.api_key)
    os.environ.setdefault("POLYMARKET_API_SECRET", creds.api_secret)
    os.environ.setdefault("POLYMARKET_PASSPHRASE", creds.api_passphrase)


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("ERROR: pip install -e .", file=sys.stderr)
        sys.exit(1)
    custom = os.environ.get("TYREX_PM_DOTENV")
    if custom:
        p = Path(custom).expanduser()
        if not p.is_file():
            print(f"ERROR: TYREX_PM_DOTENV missing: {p}", file=sys.stderr)
            sys.exit(1)
        load_dotenv(p, override=False)
        return
    default = REPO_ROOT / ".env"
    if default.is_file():
        load_dotenv(default, override=False)


def _dump_cache_instrument(
    node: object,
    instrument_id: object,
    log: Callable[[str], None],
) -> None:
    inst = node.cache.instrument(instrument_id)
    log(
        f"spike_cache: cache.instrument({instrument_id}) -> "
        f"{'OK ' + str(inst.id) if inst is not None else 'MISSING'}",
    )


def _dump_cache_orders(node: object, log: Callable[[str], None]) -> None:
    """Read Nautilus-visible open orders via CacheFacade (package API)."""
    cache = node.cache
    orders_open = cache.orders_open()
    log(f"spike_cache: orders_open() count={len(orders_open)}")
    for o in orders_open:
        vid = getattr(o, "venue_order_id", None)
        log(
            f"spike_cache: client_order_id={o.client_order_id} "
            f"venue_order_id={vid} status={o.status} "
            f"side={o.side} qty={o.quantity} price={getattr(o, 'price', None)} "
            f"instrument_id={o.instrument_id}",
        )


async def _run_timed_spike(
    *,
    node: object,
    instrument_id: object,
    wait_secs: float,
    log: Callable[[str], None],
) -> None:
    """
    Run TradingNode until stopped: start run_async in background, wait, dump cache, stop.

    ``TradingNode`` **must** have been constructed with ``loop=asyncio.get_running_loop()``
    (same loop as ``asyncio.run``). Otherwise the kernel uses another loop →
    "Started when loop is not running", exec client never connects, and
    ``Future attached to a different loop`` on shutdown (**Windows especially**).
    """
    run_task = asyncio.create_task(node.run_async())
    try:
        await asyncio.sleep(wait_secs)
        log("spike: post-wait cache snapshot")
        _dump_cache_instrument(node, instrument_id, log)
        _dump_cache_orders(node, log)
    finally:
        log("spike: calling stop_async")
        await node.stop_async()
        try:
            await asyncio.wait_for(run_task, timeout=60.0)
        except asyncio.TimeoutError:
            log("spike: run_async still running after stop; cancelling task")
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run_task
        except asyncio.CancelledError:
            log("spike: run_async ended with CancelledError")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Path A spike: TradingNode + Polymarket data + exec clients + framework limit order."
        ),
    )
    parser.add_argument(
        "--instrument-id",
        default=os.environ.get("TYREX_SPIKE_INSTRUMENT_ID"),
        help="Full Nautilus id e.g. condition-token.POLYMARKET (see polymarket symbol.py)",
    )
    parser.add_argument(
        "--price",
        default=os.environ.get("TYREX_SPIKE_PRICE", "0.05"),
        help="Limit price as decimal string (Polymarket probability)",
    )
    parser.add_argument(
        "--quantity",
        default=os.environ.get("TYREX_SPIKE_QUANTITY", "5"),
        help="Order size as decimal string (shares)",
    )
    parser.add_argument(
        "--side",
        choices=("BUY", "SELL"),
        default=os.environ.get("TYREX_SPIKE_SIDE", "BUY"),
    )
    parser.add_argument(
        "--wait-secs",
        type=float,
        default=float(os.environ.get("TYREX_SPIKE_WAIT_SECS", "150")),
        help=(
            "Seconds before cache snapshot + stop. TradingNode may await client "
            "connection ~120s by default — use at least 130 for a live check."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate imports and config only; no network / no TradingNode.run",
    )
    args = parser.parse_args()

    _load_dotenv()

    from nautilus_trader.adapters.polymarket import (
        POLYMARKET,
        POLYMARKET_CLIENT_ID,
        PolymarketDataClientConfig,
        PolymarketExecClientConfig,
        PolymarketLiveDataClientFactory,
        PolymarketLiveExecClientFactory,
    )
    from nautilus_trader.common import Environment
    from nautilus_trader.common.config import InstrumentProviderConfig, LoggingConfig
    from nautilus_trader.config import RoutingConfig
    from nautilus_trader.live.config import TradingNodeConfig
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.enums import OrderSide, TimeInForce
    from nautilus_trader.model.identifiers import InstrumentId, TraderId
    from nautilus_trader.trading.config import StrategyConfig
    from nautilus_trader.trading.strategy import Strategy

    if not args.instrument_id:
        print(
            "ERROR: --instrument-id or TYREX_SPIKE_INSTRUMENT_ID required "
            "(format: {condition_id}-{token_id}.POLYMARKET)",
            file=sys.stderr,
        )
        return 1

    instrument_id = InstrumentId.from_str(args.instrument_id)
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))

    # Repo pattern: optional funder for sig 1/2 (matches clob_factory / verify_polymarket_auth).
    funder = os.environ.get("POLYMARKET_FUNDER")

    # **Package-source-confirmed:** both factories read ``config.instrument_provider`` into
    # separate ``PolymarketInstrumentProvider`` instances; use identical ``InstrumentProviderConfig``
    # so load_ids match (**Examples-confirmed:** ``polymarket_exec_tester.py``).
    instrument_provider_cfg = InstrumentProviderConfig(load_ids=frozenset({instrument_id}))

    routing = RoutingConfig(default=True, venues=frozenset({POLYMARKET}))

    data_cfg = PolymarketDataClientConfig(
        signature_type=sig_type,
        funder=funder,
        instrument_provider=instrument_provider_cfg,
        routing=routing,
    )

    exec_cfg = PolymarketExecClientConfig(
        signature_type=sig_type,
        funder=funder,
        instrument_provider=instrument_provider_cfg,
        routing=routing,
    )

    node_cfg = TradingNodeConfig(
        trader_id=TraderId("TYREX-SPIKE-001"),
        environment=Environment.LIVE,
        logging=LoggingConfig(log_level=os.environ.get("TYREX_SPIKE_LOG_LEVEL", "INFO")),
        data_clients={f"{POLYMARKET}-SPIKE-DATA": data_cfg},
        exec_clients={f"{POLYMARKET}-SPIKE": exec_cfg},
        load_state=False,
        save_state=False,
    )

    class SpikePolymarketExecConfig(StrategyConfig, frozen=True):
        instrument_id: InstrumentId
        price: str
        quantity: str
        order_side: str = "BUY"

    class SpikePolymarketExecStrategy(Strategy):
        """
        Minimal strategy: submit one limit order on start via framework path only.
        Spike-only — not for production.
        """

        def __init__(self, config: SpikePolymarketExecConfig) -> None:
            super().__init__(config)

        def on_start(self) -> None:
            self.log.info("spike_strategy: on_start")
            inst = self.cache.instrument(self.config.instrument_id)
            if inst is None:
                self.log.error(
                    f"spike_strategy: no instrument in cache for {self.config.instrument_id}",
                )
                return
            side = (
                OrderSide.BUY
                if self.config.order_side.upper() == "BUY"
                else OrderSide.SELL
            )
            order = self.order_factory.limit(
                instrument_id=self.config.instrument_id,
                order_side=side,
                quantity=inst.make_qty(Decimal(self.config.quantity)),
                price=inst.make_price(Decimal(self.config.price)),
                time_in_force=TimeInForce.GTC,
            )
            self.submit_order(order, client_id=POLYMARKET_CLIENT_ID)
            self.log.info(
                f"spike_strategy: submit_order dispatched client_order_id={order.client_order_id}",
            )

    strat_cfg = SpikePolymarketExecConfig(
        instrument_id=instrument_id,
        price=args.price,
        quantity=args.quantity,
        order_side=args.side,
    )

    if args.dry_run:
        print("dry_run: TradingNodeConfig and strategy config constructed OK.")
        print(f"dry_run: instrument_id={instrument_id}")
        print(
            f"dry_run: shared InstrumentProviderConfig load_ids="
            f"{sorted(instrument_provider_cfg.load_ids or (), key=str)}",
        )
        print(f"dry_run: data_clients key={POLYMARKET}-SPIKE-DATA")
        print(f"dry_run: exec_clients key={POLYMARKET}-SPIKE")
        return 0

    if os.environ.get("TYREX_SPIKE_CONFIRM") != "I_UNDERSTAND":
        print(
            "ERROR: Live spike refused. Set TYREX_SPIKE_CONFIRM=I_UNDERSTAND "
            "and ensure POLYMARKET_PK / L2 creds are valid.",
            file=sys.stderr,
        )
        return 1

    if not os.environ.get("POLYMARKET_PK"):
        print("ERROR: POLYMARKET_PK not set.", file=sys.stderr)
        return 1

    _ensure_polymarket_l2_env_for_nautilus_factory()

    def _log(msg: str) -> None:
        print(msg, flush=True)

    async def _async_main() -> None:
        loop = asyncio.get_running_loop()
        node = TradingNode(config=node_cfg, loop=loop)
        node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
        node.add_exec_client_factory(POLYMARKET, PolymarketLiveExecClientFactory)
        node.trader.add_strategy(SpikePolymarketExecStrategy(strat_cfg))
        node.build()
        await _run_timed_spike(
            node=node,
            instrument_id=instrument_id,
            wait_secs=args.wait_secs,
            log=_log,
        )

    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: spike failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise

    _log("spike: finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
