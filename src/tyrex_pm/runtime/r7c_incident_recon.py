"""Read-only R7C / R7C.1 incident and FLAT reconciliation (no mutations)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.execution.polymarket.address_roles import roles_from_credentials
from tyrex_pm.execution.polymarket.auth import load_l2_credentials, redact_text
from tyrex_pm.execution.polymarket.sdk_readonly import SdkReadonlyTransport
from tyrex_pm.execution.polymarket.settlement import (
    DEFAULT_MIN_TRADABLE,
    FlatClassification,
    classify_flatness,
)
from tyrex_pm.runtime.r7_position_ack import (
    read_acknowledgment,
    validate_acknowledgment_against_inventory,
)


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
    }


def run_incident_recon(
    *,
    buy_order_id: str = INCIDENT_BUY_ORDER,
    condition_id: str = INCIDENT_CONDITION,
    token_id: str = INCIDENT_TOKEN,
    market_slug: str = INCIDENT_SLUG,
    acknowledgment_path: Path | None = None,
    output_path: Path | None = None,
    repo_root: Path | None = None,
    min_tradable: Decimal = DEFAULT_MIN_TRADABLE,
) -> dict[str, Any]:
    """Authenticated read-only reconstruction. Never mutates."""
    repo = repo_root or Path.cwd()
    env = _load_env(repo)
    creds = load_l2_credentials(env)
    roles = roles_from_credentials(creds)
    roles.assert_conditional_query_target(queried_as=roles.conditional_owner)
    transport = SdkReadonlyTransport.from_env(env)

    report: dict[str, Any] = {
        "schema": "r7c1_incident_recon_v1",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mutations_attempted": False,
        "market_slug": market_slug,
        "condition_id": condition_id,
        "token_suffix": token_id[-8:],
        "buy_order_id": buy_order_id,
        "address_roles": roles.to_safe_dict(),
        "settlement_finality_rule": "CONFIRMED_ONLY_PLUS_CONDITIONAL_BALANCE",
    }

    try:
        buy_order = transport.get_order(buy_order_id)
        report["buy_order"] = None if buy_order is None else _order_to_safe(buy_order)
    except Exception as exc:  # noqa: BLE001
        report["buy_order_error"] = redact_text(f"{type(exc).__name__}:{exc}", creds)

    trades_safe: list[dict[str, Any]] = []
    try:
        trades = transport.get_trades(market_id=condition_id)
        if not trades:
            trades = transport.get_trades()
        for t in trades:
            if (
                t.venue_order_id == buy_order_id
                or t.instrument_token_id == token_id
                or t.market_id == condition_id
            ):
                trades_safe.append(_trade_to_safe(t))
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

    buy_trades = [t for t in trades_safe if t.get("order_id") == buy_order_id]
    if not buy_trades:
        buy_trades = [t for t in trades_safe if str(t.get("side", "")).upper() == "BUY"]
    sell_trades = [t for t in trades_safe if str(t.get("side", "")).upper() == "SELL"]

    acquired = sum((Decimal(str(t["size"])) for t in buy_trades), Decimal("0"))
    sold = sum((Decimal(str(t["size"])) for t in sell_trades), Decimal("0"))
    statuses = [str(t.get("status") or "").upper() for t in buy_trades]
    report["buy_fill_summary"] = {
        "trade_count": len(buy_trades),
        "acquired_quantity": str(acquired),
        "statuses": statuses,
        "has_matched": any(s == "MATCHED" for s in statuses),
        "has_mined": any(s == "MINED" for s in statuses),
        "has_confirmed": any(s == "CONFIRMED" for s in statuses),
        "inventory_counts_confirmed_only": True,
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
        "note": "includes manual UI SELL when present",
    }

    # Data API positions (full rows for ack)
    raw_positions = transport.get_positions_raw()
    selected_pos = [
        r
        for r in raw_positions
        if str(r.get("asset") or "") == token_id
        or str(r.get("conditionId") or "") == condition_id
    ]
    data_api_selected_qty = sum(
        (Decimal(str(r.get("size") or "0")) for r in selected_pos), Decimal("0")
    )
    report["data_api_selected_market"] = {
        "positions": [
            {
                "token_suffix": str(r.get("asset") or "")[-8:],
                "size": str(r.get("size")),
                "condition_id": r.get("conditionId"),
            }
            for r in selected_pos
        ],
        "flat_by_data_api": data_api_selected_qty == 0,
        "quantity": str(data_api_selected_qty),
    }

    try:
        opens = transport.get_open_orders(market_id=condition_id)
    except Exception:  # noqa: BLE001
        opens = [
            o
            for o in transport.get_open_orders()
            if o.market_id == condition_id or o.instrument_token_id == token_id
        ]
    report["selected_market_open_orders"] = [_order_to_safe(o) for o in opens]
    report["selected_market_open_orders_zero"] = len(opens) == 0

    # Authoritative conditional balance (funder/proxy)
    bal_known = False
    bal_shares: Decimal | None = None
    try:
        bal_shares, allowance = transport.get_conditional_balance_allowance(token_id)
        bal_known = True
        report["conditional_balance"] = {
            "ok": True,
            "balance_shares": str(bal_shares),
            "allowance_shares": None if allowance is None else str(allowance),
            "query_owner_fp": roles.to_safe_dict()["conditional_owner_fp"],
            "authoritative_for_execution_safety": True,
        }
    except Exception as exc:  # noqa: BLE001
        report["conditional_balance"] = {
            "ok": False,
            "error": type(exc).__name__,
            "detail": redact_text(str(exc)[:200], creds),
        }

    mark = None
    if buy_trades:
        mark = Decimal(str(buy_trades[0]["price"]))
    flatness = classify_flatness(
        conditional_balance=bal_shares,
        balance_known=bal_known,
        min_tradable=min_tradable,
        mark_price=mark,
    )
    report["flat_classification"] = flatness
    report["selected_market_flat"] = (
        flatness["classification"] == FlatClassification.FLAT.value
    )
    report["observations"] = {
        "data_api_flat": data_api_selected_qty == 0,
        "conditional_balance_shares": None if bal_shares is None else str(bal_shares),
        "authoritative": "conditional_balance",
        "disagreement": (
            data_api_selected_qty == 0
            and bal_known
            and bal_shares is not None
            and bal_shares > 0
        ),
    }

    # Ack — full Data API rows; exclude selected-market lifecycle token from set
    if acknowledgment_path and acknowledgment_path.exists():
        ack = read_acknowledgment(acknowledgment_path)
        v = validate_acknowledgment_against_inventory(
            ack,
            raw_positions=raw_positions,
            selected_token_ids=[token_id],
            selected_condition_id=condition_id,
            ignore_selected_market_tokens=[token_id],
        )
        report["acknowledgment"] = {
            "id": ack.acknowledgment_id,
            "ok": v.ok,
            "matched": v.matched,
            "blockers": v.blockers,
            "notes": v.notes,
            "expected": ack.expected_position_count,
            "visible_account_wide": True,
            "positions": [
                {
                    "token_suffix": p.token_id[-8:],
                    "condition_suffix": p.condition_id[-8:],
                    "quantity": p.quantity,
                    "category": p.category,
                    "untouched": True,
                }
                for p in ack.positions
            ],
        }

    report["terminal"] = flatness["classification"]
    report["root_cause_ranking"] = [
        {
            "rank": 1,
            "hypothesis": "MATCHED_TO_SETTLEMENT_BALANCE_VISIBILITY_DELAY",
            "confidence": "high",
        },
        {
            "rank": 2,
            "hypothesis": "CLOB_CONDITIONAL_BALANCE_CACHE_DELAY",
            "confidence": "medium",
        },
    ]

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
