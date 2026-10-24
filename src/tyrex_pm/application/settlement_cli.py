"""``tyrex-pm settlement`` — operator commands over the durable settlement ledger.

Offline:  status, worklist, mark-manual, ack-loss-pause
Chain (read-only, ``--connect-chain`` + ``TYREX_POLYGON_RPC_URL``): reconcile,
confirm-redemption, discover-redemptions. Nothing here signs, redeems, transfers or
submits orders; the operator redeems in the venue UI. ``discover-redemptions`` finds and
proves those redemptions itself (a running runner does the same in the background);
``confirm-redemption`` proves one by its transaction hash.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from tyrex_pm.execution.settlement.ledger import SettlementLedger
from tyrex_pm.execution.settlement.resolution import ResolutionManager
from tyrex_pm.runtime.run_config import RunConfigError, load_trading_run_config


def add_parser(commands: Any) -> None:
    st = commands.add_parser("settlement", help="Hold-to-resolution ledger operator commands")
    st.add_argument("--config", type=Path, required=True, help="hold_to_resolution run YAML")
    st.add_argument("--dotenv", type=Path, default=Path(".env"))
    sub = st.add_subparsers(dest="settlement_command", required=True)
    sub.add_parser("status", help="Episodes, buckets and trial budget (offline)")
    sub.add_parser("worklist", help="Positions awaiting operator redemption (offline)")
    rec = sub.add_parser("reconcile", help="Read finalized payouts for committed holdings")
    rec.add_argument("--connect-chain", action="store_true")
    conf = sub.add_parser("confirm-redemption", help="Prove one redemption by its tx hash")
    conf.add_argument("--episode", required=True)
    conf.add_argument("--tx-hash", required=True)
    conf.add_argument("--connect-chain", action="store_true")
    disc = sub.add_parser(
        "discover-redemptions",
        help="Find and prove UI redemptions on chain (dry run unless --apply; refused while "
        "a runner owns the ledger, which discovers them itself)",
    )
    disc.add_argument("--connect-chain", action="store_true")
    disc.add_argument("--apply", action="store_true",
                      help="record proven, unambiguous redemptions in the ledger")
    disc.add_argument("--force", action="store_true", help="ignore the error backoff")
    manual = sub.add_parser("mark-manual", help="Record operator intervention for an episode")
    manual.add_argument("--episode", required=True)
    manual.add_argument("--reason", required=True)
    ack = sub.add_parser(
        "ack-loss-pause",
        help="Acknowledge a consecutive-loss pause (losses before this no longer count)",
    )
    ack.add_argument("--reason", required=True)
    repair = sub.add_parser(
        "repair-attempt-ownership",
        help="Verify and correct a window whose fill was mis-owned by a never-sent attempt "
        "(dry run unless --apply; never trades or redeems)",
    )
    repair.add_argument("--window", required=True, help="window id, e.g. btc-updown-5m-...")
    repair.add_argument("--apply", action="store_true", help="append the corrections")
    repair.add_argument("--connect-chain", action="store_true",
                        help="read the finalized payout (established resolution workflow)")


def _ledger(config: Any) -> SettlementLedger:
    return SettlementLedger(config.settlement.ledger_path, read_only=True)


def _status(ledger: SettlementLedger, config: Any) -> dict:
    totals = ledger.totals()
    trial = config.settlement.trial
    return {
        "ledger": str(config.settlement.ledger_path),
        "torn_tail_skipped": ledger.torn_tail_skipped,
        "snapshot_at": ledger.snapshot_at,
        "through_sequence": ledger._seq,
        "episodes": [
            {
                "episode_id": e.episode_id,
                "state": None if e.state is None else e.state.value,
                "window_id": e.window_id,
                "net_shares": str(e.net_shares),
                "cost_basis": str(e.cost_basis),
                "reserved": str(e.reserved_debit + e.reserved_fees),
                "receivable": str(e.receivable),
                "realized_pnl": None if e.realized_pnl is None else str(e.realized_pnl),
            }
            for e in ledger.episodes.values()
        ],
        "totals": {k: str(v) for k, v in totals.__dict__.items()},
        "trial_limits": {k: str(v) for k, v in trial.__dict__.items()},
        "run_limits": None
        if config.settlement.run_limits is None
        else {k: str(v) for k, v in config.settlement.run_limits.__dict__.items()},
        "consecutive_losses": ledger.consecutive_losses(),
        **_daily(ledger),
        "loss_budget_used": str(totals.realized_losses + totals.open_worst_case_loss),
    }


def _daily(ledger: SettlementLedger) -> dict:
    """UTC accounting day (authoritative) with its Paris-time presentation."""
    from datetime import datetime, timezone

    from tyrex_pm.reporting.timezones import paris_iso

    now = datetime.now(timezone.utc)
    return {
        "accounting_day_utc": now.date().isoformat(),
        "reported_at_utc": now.isoformat(),
        "reported_at_paris": paris_iso(now),
        "realized_losses_today_utc": str(ledger.realized_losses_on(now.date())),
    }


def _rpc_url(dotenv: Path) -> str:
    from tyrex_pm.adapters.polymarket.polygon_rpc import POLYGON_RPC_ENV

    url = os.environ.get(POLYGON_RPC_ENV, "")
    if not url and dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{POLYGON_RPC_ENV}="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not url:
        raise RuntimeError(f"{POLYGON_RPC_ENV} is not configured")
    return url


def _discover(config: Any, args: argparse.Namespace, ledger: SettlementLedger) -> int:
    from tyrex_pm.execution.settlement.chain import SettlementChainReader
    from tyrex_pm.execution.settlement.redemption_discovery import (
        DiscoveryPolicy,
        RedemptionDiscovery,
    )

    wallet = config.settlement.redemption_wallet
    if not wallet:
        print("settlement.redemption_wallet is not configured", file=sys.stderr)
        return 2
    reader = SettlementChainReader(_rpc_url(args.dotenv), timeout_s=15.0, attempts=4,
                                   retry_delay_s=1.0)
    manager = ResolutionManager(ledger, payout_reader=_no_reader, wallet=wallet)
    options = config.settlement.redemption_discovery
    discovery = RedemptionDiscovery(
        manager, reader, wallet=wallet,
        state_path=config.settlement.ledger_path.parent / "redemption_discovery.json",
        policy=DiscoveryPolicy(interval_s=options.interval_s,
                               max_blocks_per_pass=options.max_blocks_per_pass,
                               max_log_calls_per_pass=options.max_log_calls_per_pass))
    report = asyncio.run(discovery.run_once(apply=args.apply, force=args.force))
    print(json.dumps(report, indent=1, default=str))
    return 0


def _chain_manager(ledger: SettlementLedger, config: Any, dotenv: Path) -> ResolutionManager:
    from tyrex_pm.adapters.polymarket.polygon_rpc import POLYGON_RPC_ENV
    from tyrex_pm.execution.settlement.chain import (
        SettlementChainReader,
        finalized_payout_reader,
        receipt_redemption_verifier,
    )

    url = os.environ.get(POLYGON_RPC_ENV, "")
    if not url and dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{POLYGON_RPC_ENV}="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not url:
        raise RuntimeError(f"{POLYGON_RPC_ENV} is not configured")
    reader = SettlementChainReader(url)
    return ResolutionManager(
        ledger,
        payout_reader=finalized_payout_reader(reader),
        redemption_verifier=receipt_redemption_verifier(reader),
        wallet=config.settlement.redemption_wallet,
    )


def run(args: argparse.Namespace) -> int:
    try:
        config = load_trading_run_config(args.config)
    except (RunConfigError, OSError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if config.settlement is None:
        print("config is not a hold_to_resolution profile", file=sys.stderr)
        return 2
    command = args.settlement_command
    from tyrex_pm.execution.process_lock import LedgerWriter, StateDirectoryBusy

    if command == "repair-attempt-ownership":
        from tyrex_pm.application import ownership_repair

        try:
            return ownership_repair.run(args)
        except StateDirectoryBusy as exc:
            print(f"settlement writer busy: {exc}", file=sys.stderr)
            return 3

    if command in ("status", "worklist"):
        return _run_command(config, args, _ledger(config))
    try:
        with LedgerWriter(config.settlement.ledger_path) as writer:
            return _run_command(config, args, writer.ledger())
    except StateDirectoryBusy as exc:
        print(f"settlement writer busy: {exc}; stop the owning runner first", file=sys.stderr)
        return 3


def _run_command(config: Any, args: argparse.Namespace, ledger: SettlementLedger) -> int:
    command = args.settlement_command
    if command == "status":
        print(json.dumps(_status(ledger, config), indent=1))
        return 0
    if command == "worklist":
        manager = ResolutionManager(ledger, payout_reader=_no_reader)
        print(json.dumps(manager.worklist(), indent=1))
        return 0
    if command == "mark-manual":
        if args.episode not in ledger.episodes:
            print(f"unknown episode {args.episode}", file=sys.stderr)
            return 2
        ledger.record(
            "MANUAL_INTERVENTION", args.episode, key=f"MANUAL:{args.episode}", reason=args.reason
        )
        print(f"episode {args.episode} marked MANUAL_INTERVENTION (blocks new entries)")
        return 0
    if command == "ack-loss-pause":
        from datetime import datetime, timezone

        from tyrex_pm.execution.settlement.ledger import OPERATOR_SCOPE

        before = ledger.consecutive_losses()
        stamp = datetime.now(timezone.utc).isoformat()
        ledger.record("LOSS_PAUSE_ACKNOWLEDGED", OPERATOR_SCOPE, key=f"LOSS_ACK:{stamp}",
                      reason=args.reason, losses_acknowledged=before)
        print(json.dumps({"acknowledged_losses": before, "reason": args.reason}))
        return 0
    if not getattr(args, "connect_chain", False):
        print(f"`{command}` reads Polygon; re-run with --connect-chain", file=sys.stderr)
        return 2
    if command == "discover-redemptions":
        return _discover(config, args, ledger)
    manager = _chain_manager(ledger, config, args.dotenv)
    if command == "reconcile":
        report = asyncio.run(manager.reconcile())
        print(json.dumps(report.__dict__, indent=1))
        print(json.dumps(manager.worklist(), indent=1))
        return 0
    if command == "confirm-redemption":
        ok, reasons = asyncio.run(manager.confirm_redemption(args.episode, args.tx_hash))
        print(json.dumps({"confirmed": ok, "reasons": list(reasons)}))
        return 0 if ok else 2
    return 2


async def _no_reader(condition_id: str, outcome_index: int):  # pragma: no cover - offline
    raise RuntimeError("offline command")
