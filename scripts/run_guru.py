#!/usr/bin/env python3
"""
Operational entrypoint: guru follow bot (shadow or live).

Usage:
  python scripts/run_guru.py \\
    --strategy-conf config/strategy/guru_follow.yaml \\
    --risk-conf config/risk/guru_follow_risk.yaml \\
    --live-conf config/runtime/live_polymarket.yaml

Secrets: repo root ``.env`` (or ``TYREX_PM_DOTENV``), never YAML.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _merge_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("ERROR: pip install -e .", file=sys.stderr)
        sys.exit(1)
    import os

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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy-conf", required=True, type=Path)
    parser.add_argument("--risk-conf", required=True, type=Path)
    parser.add_argument("--live-conf", required=True, type=Path)
    args = parser.parse_args()

    _merge_dotenv()

    from tyrex_pm.config.loaders import (
        load_risk_settings,
        load_runtime_settings,
        load_strategy_settings,
    )
    from tyrex_pm.runtime.guru_compose import build_guru_trading_node

    try:
        strat = load_strategy_settings(args.strategy_conf)
        risk = load_risk_settings(args.risk_conf)
        runtime = load_runtime_settings(args.live_conf)
    except ValueError as exc:
        print(f"ERROR: config validation failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"tyrex_pm guru run | mode={runtime.execution_mode} | "
        f"trader_id={runtime.trader_id} | guru={strat.guru_wallet_address[:10]}… | "
        f"allowlist={len(strat.allowlisted_token_ids)} token(s)"
    )

    node, _risk_pol = build_guru_trading_node(strat, risk, runtime)
    node.build()
    try:
        node.run(raise_exception=True)
    except KeyboardInterrupt:
        print("\nStopping…")
        node.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
