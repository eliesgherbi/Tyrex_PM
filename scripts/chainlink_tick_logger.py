#!/usr/bin/env python3
"""A0.5 Chainlink tick logger sidecar — continuous RTDS tick capture for PTB."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from tyrex_pm.runtime.chainlink_tick_logger import (
    DEFAULT_CHAINLINK_TICKS_PATH,
    DEFAULT_RETENTION_S,
    ChainlinkTickLoggerConfig,
    run_chainlink_tick_logger,
)

log = logging.getLogger(__name__)


async def _main_async(args: argparse.Namespace) -> int:
    stop = asyncio.Event()
    cfg = ChainlinkTickLoggerConfig(
        path=Path(args.output),
        retention_s=args.retention_s,
        feeds=tuple(args.feeds.split(",")),
        symbols=tuple(args.symbols.split(",")),
    )
    log.info("chainlink tick logger starting path=%s retention_s=%s", cfg.path, cfg.retention_s)
    try:
        await run_chainlink_tick_logger(stop=stop, config=cfg)
    except KeyboardInterrupt:
        stop.set()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Chainlink tick logger sidecar (A0.5)")
    parser.add_argument("--output", type=str, default=str(DEFAULT_CHAINLINK_TICKS_PATH))
    parser.add_argument("--retention-s", type=float, default=DEFAULT_RETENTION_S)
    parser.add_argument("--feeds", default="chainlink")
    parser.add_argument("--symbols", default="btc/usd")
    args = parser.parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
