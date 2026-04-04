#!/usr/bin/env python3
"""
Operational entrypoint: guru follow bot (shadow or live).

Usage:
  python scripts/run_guru.py \\
    --strategy-conf config/strategy/guru_follow.yaml \\
    --risk-conf config/risk/guru_follow_risk.yaml \\
    --live-conf config/runtime/live_polymarket.yaml

Tyrex and Nautilus logs are written to separate files under ``logs/<mode>/`` by default
(see ``--log-name``). Console output is unchanged.

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
    parser.add_argument(
        "--log-name",
        default=None,
        metavar="NAME",
        help=(
            "Optional basename for per-source logs: logs/<mode>/NAME_tyrex.log and "
            "NAME_nautilus.log. Default stem run → run_tyrex.log / run_nautilus.log. "
            "Only letters, digits, and ._- between segments; max 100 chars."
        ),
    )
    args = parser.parse_args()

    _merge_dotenv()

    import logging

    if not logging.root.handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("tyrex_pm").setLevel(logging.INFO)

    from tyrex_pm.config.loaders import (
        load_risk_settings,
        load_runtime_settings,
        load_strategy_settings,
    )
    from tyrex_pm.runtime.guru_compose import build_guru_trading_node
    from tyrex_pm.runtime.guru_run_logging import (
        GuruNautilusFileLogging,
        announce_guru_run_log_destinations,
        attach_tyrex_pm_file_handler,
        ensure_guru_run_log_dir,
        resolve_guru_source_log_path,
    )

    try:
        strat = load_strategy_settings(args.strategy_conf)
        risk = load_risk_settings(args.risk_conf)
        runtime = load_runtime_settings(args.live_conf)
    except ValueError as exc:
        print(f"ERROR: config validation failed: {exc}", file=sys.stderr)
        return 1

    try:
        tyrex_log_path = resolve_guru_source_log_path(
            REPO_ROOT, runtime.execution_mode, args.log_name, "tyrex"
        )
        nautilus_log_path = resolve_guru_source_log_path(
            REPO_ROOT, runtime.execution_mode, args.log_name, "nautilus"
        )
    except ValueError as exc:
        print(f"ERROR: invalid --log-name: {exc}", file=sys.stderr)
        return 1
    ensure_guru_run_log_dir(nautilus_log_path)
    attach_tyrex_pm_file_handler(tyrex_log_path)
    announce_guru_run_log_destinations(tyrex_log_path, nautilus_log_path)

    tf = strat.token_filter
    tf_desc = (
        f"token_filter=on ({len(tf.allowlisted_token_ids)} ids)"
        if tf.enabled
        else "token_filter=off (all guru tokens)"
    )
    print(
        f"tyrex_pm guru run | mode={runtime.execution_mode} | "
        f"trader_id={runtime.trader_id} | guru={strat.guru_wallet_address[:10]}… | "
        f"{tf_desc}"
    )
    if (
        runtime.execution_mode == "live"
        and runtime.polymarket_nautilus_live
        and runtime.polymarket_framework_submit
    ):
        print(
            "phase_a: pending=Cache open orders (leaves qty); "
            f"filled cap=Portfolio net_exposure; capital_gate="
            f"{'on' if risk.capital_gate_enabled else 'off'}. "
            "Restart: Nautilus load/save state disabled — "
            "see Docs/Implementation/phase_a_closure.md",
        )

    assembly = build_guru_trading_node(
        strat,
        risk,
        runtime,
        nautilus_file_logging=GuruNautilusFileLogging(
            log_directory=str(nautilus_log_path.parent.resolve()),
            log_file_stem=nautilus_log_path.stem,
        ),
    )
    node = assembly.node
    node.build()
    try:
        node.run(raise_exception=True)
    except KeyboardInterrupt:
        print("\nStopping…")
        node.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
