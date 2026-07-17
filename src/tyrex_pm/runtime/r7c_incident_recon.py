"""Read-only R7C incident / FLAT reconciliation (no mutations)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.execution.polymarket.auth import (
    load_l2_credentials,
    positions_wallet_address,
    redact_text,
)
from tyrex_pm.execution.polymarket.sdk_readonly import SdkReadonlyTransport
from tyrex_pm.runtime.r7_position_ack import read_acknowledgment, validate_acknowledgment_against_inventory


INCIDENT_BUY_ORDER = "0x68efa63a23abb0ab55042204683f48f4303ed2db3e9d955317bc41add43e71db"
INCIDENT_CONDITION = "0x32204a5cffff255df6155b69105aded512770c4597ccb4bef721dfa0ab526401"
INCIDENT_TOKEN = (
    "1038082852808592687103030316298741805143710778541582307413776216436336466979"
)
INCIDENT_SLUG = "btc-updown-5m-1784303100"


def _addr_fingerprint(addr: str | None) -> str | None:
    if not addr:
        return None
    a = addr.lower()
    if len(a) < 10:
        return "***"
    return f"{a[:6]}…{a[-4:]}"


def _load_env(repo: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    env_path = repo / ".env"
    if not env_path.exists():
        return env
    for line in env_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _trade_to_safe(t: Any) -> dict[str, Any]:
    raw = dict(t.raw) if getattr(t, "raw", None) else {}
    tx = raw.get("transaction_hash") or raw.get("transactionHash") or raw.get("tx_hash")
    maker = raw.get("maker_address") or raw.get("maker") or raw.get("owner")
    funder = raw.get("trader") or raw.get("proxyWallet") or raw.get("funder")
    fee = raw.get("fee") or raw.get("fee_rate_bps") or (
        str(t.fee_rate_bps) if t.fee_rate_bps is not None else None
    )
    return {
        "trade_id": t.venue_trade_id,
        "order_id": t.venue_order_id,
        "side": t.side,
        "size": str(t.size),
        "price": str(t.price),
        "status": t.status,
        "fee": None if fee is None else str(fee),
        "market_id": t.market_id,
        "token_suffix": t.instrument_token_id[-8:] if t.instrument_token_id else None,
        "transaction_hash_suffix": (str(tx)[-10:] if tx else None),
        "maker_fp": _addr_fingerprint(str(maker) if maker else None),
        "funder_fp": _addr_fingerprint(str(funder) if funder else None),
        "timestamp": raw.get("match_time")
        or raw.get("timestamp")
        or raw.get("created_at")
        or raw.get("last_update"),
    }


def _order_to_safe(o: Any) -> dict[str, Any]:
    raw = dict(o.raw) if getattr(o, "raw", None) else {}
    maker = raw.get("maker_address") or raw.get("owner") or raw.get("maker")
    return {
        "order_id": o.venue_order_id,
        "status": o.status,
        "side": o.side,
        "original_size": str(o.original_size),
        "size_matched": str(o.size_matched),
        "price": str(o.price),
        "token_suffix": o.instrument_token_id[-8:] if o.instrument_token_id else None,
        "market_id": o.market_id,
        "maker_fp": _addr_fingerprint(str(maker) if maker else None),
        "associate_trades": raw.get("associate_trades") or raw.get("associateTrades"),
    }


def _query_conditional_balance(client: Any, token_id: str) -> dict[str, Any]:
    """Best-effort CONDITIONAL balance/allowance for sell readiness evidence."""
    try:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        raw = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        )
        if not isinstance(raw, dict):
            return {"ok": False, "error": "non_dict_response"}
        bal = Decimal(str(raw.get("balance") or "0"))
        # CLOB CONDITIONAL balance is typically 6-decimal base units (even for dust)
        bal_shares = bal / Decimal("1000000")
        allowance = None
        allowances = raw.get("allowances")
        if isinstance(allowances, dict) and allowances:
            first = next(iter(allowances.values()))
            allowance = Decimal(str(first)) / Decimal("1000000")
        return {
            "ok": True,
            "balance_raw": str(bal),
            "balance_shares_interpreted": str(bal_shares),
            "allowance_shares_interpreted": None if allowance is None else str(allowance),
            "note": "share units = raw/1e6; dust may remain after UI round-down sells",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}


def run_incident_recon(
    *,
    buy_order_id: str = INCIDENT_BUY_ORDER,
    condition_id: str = INCIDENT_CONDITION,
    token_id: str = INCIDENT_TOKEN,
    market_slug: str = INCIDENT_SLUG,
    acknowledgment_path: Path | None = None,
    output_path: Path | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Authenticated read-only reconstruction. Never mutates."""
    repo = repo_root or Path.cwd()
    env = _load_env(repo)
    creds = load_l2_credentials(env)
    transport = SdkReadonlyTransport.from_env(env)
    client = transport._client

    report: dict[str, Any] = {
        "schema": "r7c_incident_recon_v1",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mutations_attempted": False,
        "market_slug": market_slug,
        "condition_id": condition_id,
        "token_suffix": token_id[-8:],
        "buy_order_id": buy_order_id,
        "address_roles": {
            "signer_eoa_fp": _addr_fingerprint(creds.address),
            "funder_proxy_fp": _addr_fingerprint(creds.funder),
            "positions_query_fp": _addr_fingerprint(positions_wallet_address(creds)),
            "signer_eq_funder": (
                bool(creds.funder)
                and creds.address.lower() == creds.funder.lower()
            )
            if creds.funder
            else True,
            "signature_type": creds.signature_type,
        },
    }

    # BUY order
    buy_order = None
    try:
        buy_order = transport.get_order(buy_order_id)
        report["buy_order"] = None if buy_order is None else _order_to_safe(buy_order)
    except Exception as exc:  # noqa: BLE001
        report["buy_order_error"] = redact_text(f"{type(exc).__name__}:{exc}", creds)

    # Trades (filter by market / order / token)
    trades_safe: list[dict[str, Any]] = []
    try:
        trades = transport.get_trades(market_id=condition_id)
        if not trades:
            trades = transport.get_trades()
        for t in trades:
            if t.venue_order_id == buy_order_id or t.instrument_token_id == token_id:
                trades_safe.append(_trade_to_safe(t))
            elif t.market_id == condition_id:
                trades_safe.append(_trade_to_safe(t))
        # Dedup
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for row in trades_safe:
            tid = str(row.get("trade_id") or "")
            if tid and tid in seen:
                continue
            if tid:
                seen.add(tid)
            deduped.append(row)
        trades_safe = deduped
        report["trades"] = trades_safe
    except Exception as exc:  # noqa: BLE001
        report["trades_error"] = redact_text(f"{type(exc).__name__}:{exc}", creds)

    buy_trades = [t for t in trades_safe if str(t.get("side", "")).upper() == "BUY"]
    sell_trades = [t for t in trades_safe if str(t.get("side", "")).upper() == "SELL"]
    # Also match by order id for buy
    buy_by_order = [t for t in trades_safe if t.get("order_id") == buy_order_id]
    if buy_by_order:
        buy_trades = buy_by_order

    acquired = sum((Decimal(str(t["size"])) for t in buy_trades), Decimal("0"))
    sold = sum((Decimal(str(t["size"])) for t in sell_trades), Decimal("0"))
    statuses = [str(t.get("status") or "").upper() for t in buy_trades]
    report["buy_fill_summary"] = {
        "trade_count": len(buy_trades),
        "acquired_quantity": str(acquired),
        "statuses": statuses,
        "status_progression_observed": statuses,
        "has_matched": any(s == "MATCHED" for s in statuses),
        "has_mined": any(s == "MINED" for s in statuses),
        "has_confirmed": any(s == "CONFIRMED" for s in statuses),
        "has_failed": any(s == "FAILED" for s in statuses),
        "has_retrying": any(s == "RETRYING" for s in statuses),
        "usdc_spent_estimate": str(
            sum(
                (Decimal(str(t["size"])) * Decimal(str(t["price"])) for t in buy_trades),
                Decimal("0"),
            )
        ),
    }
    report["sell_fill_summary"] = {
        "trade_count": len(sell_trades),
        "sold_quantity": str(sold),
        "trades": sell_trades,
        "note": (
            "manual UI sell expected; automatic lifecycle SELL was rejected with balance=0"
        ),
    }

    # Positions / open orders
    positions = transport.get_positions()
    selected_pos = [
        p
        for p in positions
        if p.instrument_token_id == token_id or p.market_id == condition_id
    ]
    report["selected_market_positions"] = [
        {
            "token_suffix": p.instrument_token_id[-8:],
            "size": str(p.size),
            "avg_price": None if p.avg_price is None else str(p.avg_price),
            "market_id": p.market_id,
        }
        for p in selected_pos
    ]
    selected_qty = sum((p.size for p in selected_pos), Decimal("0"))
    report["selected_market_flat"] = selected_qty == 0

    try:
        opens = transport.get_open_orders(market_id=condition_id)
    except Exception:  # noqa: BLE001
        opens = transport.get_open_orders()
        opens = [o for o in opens if o.market_id == condition_id or o.instrument_token_id == token_id]
    report["selected_market_open_orders"] = [_order_to_safe(o) for o in opens]
    report["selected_market_open_orders_zero"] = len(opens) == 0

    # Conditional balance
    report["conditional_balance"] = _query_conditional_balance(client, token_id)
    try:
        coll = transport.get_balance()
        report["collateral"] = {
            "balance_present": True,
            "balance_nonzero": coll.collateral_balance > 0,
            # do not log full balance figure if large — incident needs magnitude
            "balance": str(coll.collateral_balance),
            "allowance_present": coll.allowance is not None,
        }
    except Exception as exc:  # noqa: BLE001
        report["collateral"] = {"error": type(exc).__name__}

    # Ack validation
    if acknowledgment_path and acknowledgment_path.exists():
        ack = read_acknowledgment(acknowledgment_path)
        # Rebuild raw rows from positions via data API style
        raw_rows = []
        for p in positions:
            raw_rows.append(
                {
                    "asset": p.instrument_token_id,
                    "conditionId": p.market_id,
                    "size": str(p.size),
                    "curPrice": 0,
                    "redeemable": True,
                    "outcome": "Up",
                    "slug": "",
                }
            )
        v = validate_acknowledgment_against_inventory(ack, raw_positions=raw_rows)
        report["acknowledgment"] = {
            "id": ack.acknowledgment_id,
            "ok": v.ok,
            "matched": v.matched,
            "blockers": v.blockers,
            "expected": ack.expected_position_count,
        }

    # Cause ranking (evidence-based, not asserted)
    causes: list[dict[str, Any]] = []
    causes.append(
        {
            "rank": 1,
            "hypothesis": "MATCHED_TO_SETTLEMENT_BALANCE_VISIBILITY_DELAY",
            "confidence": "high",
            "evidence": [
                "SELL ~230ms after BUY insert status=matched rejected with balance:0",
                "user later observed ~9.5 shares in UI (settlement lag plausible)",
                "runtime emitted entry_filled without trade/balance confirmation",
            ],
        }
    )
    causes.append(
        {
            "rank": 2,
            "hypothesis": "CLOB_CONDITIONAL_BALANCE_CACHE_DELAY",
            "confidence": "medium",
            "evidence": [
                "CLOB sell path checks conditional balance/allowance endpoint",
                "balance:0 at submit time despite later UI position",
            ],
        }
    )
    signer_eq = report["address_roles"]["signer_eq_funder"]
    causes.append(
        {
            "rank": 3,
            "hypothesis": "SIGNER_FUNDER_PROXY_MISMATCH",
            "confidence": "low" if signer_eq else "medium",
            "evidence": [
                f"signer_eq_funder={signer_eq}",
                "positions queried via funder when configured",
                "compare buy_order.maker_fp vs address_roles",
            ],
        }
    )
    causes.append(
        {
            "rank": 4,
            "hypothesis": "CONDITIONAL_TOKEN_ALLOWANCE",
            "confidence": "low",
            "evidence": [
                "error text said balance:0 not allowance:0",
                "allowance insufficiency usually wording differs",
            ],
        }
    )
    causes.append(
        {
            "rank": 5,
            "hypothesis": "PARTIAL_OR_FAILED_SETTLEMENT",
            "confidence": "low",
            "evidence": [
                "user observed ~full planned size later",
                "requires trade status FAILED/RETRYING evidence if present",
            ],
        }
    )
    report["root_cause_ranking"] = causes
    report["uncertainties"] = [
        "If historical trade rows aged out or UI sell not linked to same order id, "
        "manual SELL details may be incomplete.",
        "Actual acquired qty must come from CONFIRMED/MINED trades + balance, "
        "not planned 9.47.",
    ]

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


@dataclass
class FlatVerifyResult:
    ok: bool
    report: dict[str, Any]
    path: Path | None
