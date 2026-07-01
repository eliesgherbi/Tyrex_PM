#!/usr/bin/env python3
"""M8 WS-primary pre-run safety and config verification (Group E/E2 Step 1)."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from tyrex_pm.runtime.config import load_app_config  # noqa: E402

CLOB_BASE = "https://clob.polymarket.com"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
        ).strip()
    except Exception:
        return "unknown"


def _positive_decimal(value) -> Decimal | None:
    try:
        n = Decimal(str(value))
    except Exception:
        return None
    return n if n > 0 else None


def _fetch_leg_book(client: httpx.Client, token_id: str) -> dict:
    resp = client.get(f"{CLOB_BASE}/book", params={"token_id": token_id})
    resp.raise_for_status()
    book = resp.json()
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = max(
        (_positive_decimal(x.get("price")) for x in bids if _positive_decimal(x.get("size"))),
        default=None,
    )
    best_ask = min(
        (_positive_decimal(x.get("price")) for x in asks if _positive_decimal(x.get("size"))),
        default=None,
    )
    ask_depth = sum(
        (_positive_decimal(x.get("size")) or Decimal("0"))
        for x in asks
        if best_ask is not None and _positive_decimal(x.get("price")) == best_ask
    )
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "ask_depth": ask_depth,
    }


def _verify_market_books(app, errors: list[str]) -> None:
    pb = app.paired_binary
    if pb is None:
        return
    min_notional = app.risk.notional.min_usd
    position_size = pb.position_size
    headers = {"User-Agent": "TyrexPM-M8-Preflight/1.0"}
    try:
        with httpx.Client(timeout=20.0, headers=headers) as client:
            for leg, tid in (("YES", str(pb.yes_token_id)), ("NO", str(pb.no_token_id))):
                leg_book = _fetch_leg_book(client, tid)
                bid = leg_book["best_bid"]
                ask = leg_book["best_ask"]
                depth = leg_book["ask_depth"]
                if bid is None or ask is None:
                    errors.append(f"{leg} leg has empty bid/ask on CLOB")
                    continue
                notional = position_size * ask
                print(
                    f"  market {leg}: bid={bid} ask={ask} depth={depth} "
                    f"notional@{position_size}={notional}"
                )
                if notional < min_notional:
                    errors.append(
                        f"{leg} notional {notional} < min_usd {min_notional} "
                        f"(ask={ask}, position_size={position_size})"
                    )
                if depth < position_size:
                    errors.append(f"{leg} ask depth {depth} < position_size {position_size}")
                spread = ask - bid
                max_spread = pb.max_spread_yes if leg == "YES" else pb.max_spread_no
                if spread > max_spread:
                    errors.append(f"{leg} spread {spread} > max_spread {max_spread}")
    except httpx.HTTPError as exc:
        errors.append(f"CLOB book fetch failed: {exc!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="M8 WS-primary preflight")
    parser.add_argument(
        "--scenario",
        default="m8_ws_primary_validation_002",
        help="Scenario name under config/scenarios/ (default: m8_ws_primary_validation_002)",
    )
    parser.add_argument(
        "--skip-market-books",
        action="store_true",
        help="Skip live CLOB book/notional verification",
    )
    args = parser.parse_args()

    strategy_path = REPO / "config" / "strategies" / "paired_binary.yaml"
    scenario_path = REPO / "config" / "scenarios" / f"{args.scenario}.yaml"
    if not scenario_path.is_file():
        print(f"Scenario not found: {scenario_path}")
        return 1

    app = load_app_config(
        repo_root=REPO,
        strategy_file=str(strategy_path.relative_to(REPO)),
        scenario_file=str(scenario_path.relative_to(REPO)),
    )
    pb = app.paired_binary
    md = app.runtime.market_data
    errors: list[str] = []

    checks = {
        "websocket.primary_enabled": md.websocket.primary_enabled is True,
        "websocket.shadow_enabled": md.websocket.shadow_enabled is False,
        "rest.poll_enabled": md.rest.poll_enabled is False,
        "rest.bootstrap_on_startup": md.rest.bootstrap_on_startup is True,
        "rest.recovery_on_reconnect": md.rest.recovery_on_reconnect is True,
        "quality.enforcement_mode": md.quality.enforcement_mode == "enforce",
        "quality.require_ws_primary_for_entry": md.quality.require_ws_primary_for_entry is True,
        "quality.allow_rest_recovery_for_entry": md.quality.allow_rest_recovery_for_entry is False,
        "market_data.enabled": md.enabled is True,
        "planner.enabled": app.execution.planner.enabled is True,
    }
    for key, ok in checks.items():
        if not ok:
            errors.append(f"config check failed: {key}")

    if pb is None:
        errors.append("paired_binary config missing")
    elif pb.position_size > 10:
        errors.append(f"position_size {pb.position_size} exceeds safe tiny cap (10)")

    print("M8 preflight")
    print(f"  git_commit: {_git_sha()}")
    print(f"  strategy: {strategy_path} sha256={_sha256(strategy_path)[:16]}...")
    print(f"  scenario: {scenario_path} sha256={_sha256(scenario_path)[:16]}...")
    if pb:
        print(f"  market_id: {pb.market_id}")
        print(f"  yes_token_id: {pb.yes_token_id}")
        print(f"  no_token_id: {pb.no_token_id}")
        print(f"  position_size: {pb.position_size}")
        print(f"  max_runtime_s: {pb.max_runtime_s}")
        print(f"  min_notional_usd: {app.risk.notional.min_usd}")

    for key, ok in checks.items():
        print(f"  [{('OK' if ok else 'FAIL')}] {key}")

    if not args.skip_market_books:
        print("\nMarket book verification (CLOB):")
        _verify_market_books(app, errors)

    if errors:
        print("\nBLOCKED:")
        for e in errors:
            print(f"  - {e}")
        return 1

    print("\nPreflight PASSED — safe to start M8 validation run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
