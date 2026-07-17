"""R7A/R7A.1 read-only market probe + report (no mutations, no approval issuance in R7A.1)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tyrex_pm.adapters.polymarket.btc_5m_window import (
    PROVENANCE,
    MarketWindowError,
    compute_lifecycle_deadlines,
    resolve_btc_5m_window,
)
from tyrex_pm.execution.polymarket.fees_fd import FeeError, parse_fd
from tyrex_pm.execution.polymarket.order_sizing import SizingError, size_buy_under_cap
from tyrex_pm.operations import next_btc_updown_slug
from tyrex_pm.runtime.r7_position_inventory import build_inventory_report
from tyrex_pm.runtime.r7_readiness import (
    EXIT_POLICY_V1,
    RECONCILIATION_POLICY_RECOMMENDATION,
    build_r7_readiness,
)


@dataclass
class ProposedTrade:
    market_title: str
    market_slug: str
    condition_id: str
    outcome_side: str
    token_id: str
    opposite_token_id: str
    tick_size: str
    min_order_size: str
    best_ask: Decimal
    ask_size: Decimal
    sized_price: Decimal
    sized_qty: Decimal
    sized_amount: Decimal
    sized_notional: Decimal
    estimated_buy_fee: Decimal
    max_collateral: Decimal
    order_type: str
    market_start: str | None
    market_end: str | None
    listed_at: str | None
    created_at: str | None
    accepting_orders: bool | None
    fee_rate: str | None
    fee_exponent: str | None
    blocked: str | None = None


def git_commit_identity(repo: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), text=True
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _get_json(url: str) -> Any:
    req = Request(url, headers={"User-Agent": "tyrex-pm-r7a1/1.0"}, method="GET")
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _empty_trade(slug: str, blocked: str) -> ProposedTrade:
    return ProposedTrade(
        market_title="unavailable",
        market_slug=slug,
        condition_id="",
        outcome_side="YES",
        token_id="",
        opposite_token_id="",
        tick_size="0.01",
        min_order_size="5",
        best_ask=Decimal("0"),
        ask_size=Decimal("0"),
        sized_price=Decimal("0"),
        sized_qty=Decimal("0"),
        sized_amount=Decimal("0"),
        sized_notional=Decimal("0"),
        estimated_buy_fee=Decimal("0"),
        max_collateral=Decimal("0"),
        order_type="FAK",
        market_start=None,
        market_end=None,
        listed_at=None,
        created_at=None,
        accepting_orders=None,
        fee_rate=None,
        fee_exponent=None,
        blocked=blocked,
    )


def probe_btc_updown_market(*, which: str = "next", now: datetime | None = None) -> ProposedTrade:
    """Public-only probe with authoritative 5m window + fee-aware sizing."""
    now = now or datetime.now(timezone.utc)
    slug = next_btc_updown_slug(now)
    events = _get_json(f"https://gamma-api.polymarket.com/events?slug={slug}")
    if not isinstance(events, list) or not events:
        return _empty_trade(slug, "MARKET_UNRESOLVED")
    ev = events[0]
    markets = ev.get("markets") or []
    if not markets:
        return _empty_trade(slug, "MARKET_UNRESOLVED")
    m = markets[0]

    try:
        window = resolve_btc_5m_window(slug=slug, event=ev, market=m, now=now)
        deadlines = compute_lifecycle_deadlines(window, now=now)
    except MarketWindowError as exc:
        t = _empty_trade(slug, str(exc))
        t.market_title = str(m.get("question") or ev.get("title") or slug)
        return t

    raw_tokens = m.get("clobTokenIds") or "[]"
    if isinstance(raw_tokens, str):
        try:
            tokens = json.loads(raw_tokens)
        except json.JSONDecodeError:
            tokens = []
    else:
        tokens = list(raw_tokens)
    if len(tokens) < 2:
        return _empty_trade(slug, "TOKEN_UNRESOLVED")

    token_id = str(tokens[0])
    opposite = str(tokens[1])
    tick = str(m.get("orderPriceMinTickSize") or "0.01")
    min_sz = str(m.get("orderMinSize") or "5")
    condition_id = str(m.get("conditionId") or "")

    # Fees from official CLOB market info
    fee = None
    fee_blocked = None
    try:
        info = _get_json(f"https://clob.polymarket.com/clob-markets/{condition_id}")
        fee = parse_fd(info, condition_id=condition_id)
        if info.get("ao") is False:
            fee_blocked = "MARKET_NOT_ACCEPTING_ORDERS"
    except (FeeError, Exception):  # noqa: BLE001
        fee_blocked = "FEE_PARAMETERS_UNKNOWN"

    try:
        book = _get_json(f"https://clob.polymarket.com/book?token_id={token_id}")
    except Exception:  # noqa: BLE001
        t = _empty_trade(slug, "BOOK_UNREACHABLE")
        t.market_title = str(m.get("question") or slug)
        t.condition_id = condition_id
        t.token_id = token_id
        t.market_start = window.market_start.isoformat()
        t.market_end = window.market_end.isoformat()
        return t

    asks = book.get("asks") or []
    if not asks:
        t = _empty_trade(slug, "ONE_SIDED_BOOK")
        t.market_title = str(m.get("question") or slug)
        t.condition_id = condition_id
        t.token_id = token_id
        t.market_start = window.market_start.isoformat()
        t.market_end = window.market_end.isoformat()
        return t

    best = min(asks, key=lambda a: Decimal(str(a.get("price") or a[0])))
    if isinstance(best, dict):
        best_ask = Decimal(str(best["price"]))
        ask_size = Decimal(str(best.get("size") or "0"))
    else:
        best_ask = Decimal(str(best[0]))
        ask_size = Decimal(str(best[1]))

    sized = None
    blocked = fee_blocked
    if fee is not None and blocked is None:
        try:
            sized = size_buy_under_cap(
                best_ask=best_ask,
                ask_size=ask_size,
                tick_size=Decimal(tick),
                min_order_size=Decimal(min_sz),
                max_buy_notional=Decimal("5.00"),
                max_limit_price=best_ask,
                order_type="FAK",
                fee=fee,
            )
        except SizingError as exc:
            blocked = str(exc)

    # Attach deadlines on trade via blocked only; report carries full deadlines
    _ = deadlines

    return ProposedTrade(
        market_title=str(m.get("question") or ev.get("title") or slug),
        market_slug=slug,
        condition_id=condition_id,
        outcome_side="YES",
        token_id=token_id,
        opposite_token_id=opposite,
        tick_size=tick,
        min_order_size=min_sz,
        best_ask=best_ask,
        ask_size=ask_size,
        sized_price=Decimal("0") if sized is None else sized.limit_price,
        sized_qty=Decimal("0") if sized is None else sized.quantity,
        sized_amount=Decimal("0") if sized is None else sized.amount,
        sized_notional=Decimal("0") if sized is None else sized.notional,
        estimated_buy_fee=Decimal("0") if sized is None else sized.estimated_buy_fee,
        max_collateral=Decimal("0") if sized is None else sized.max_collateral,
        order_type="FAK",
        market_start=window.market_start.isoformat(),
        market_end=window.market_end.isoformat(),
        listed_at=window.listed_at.isoformat() if window.listed_at else None,
        created_at=window.created_at.isoformat() if window.created_at else None,
        accepting_orders=window.accepting_orders,
        fee_rate=str(fee.fee_rate) if fee else None,
        fee_exponent=str(fee.exponent) if fee else None,
        blocked=blocked,
    )


def _fetch_raw_positions(user: str) -> list[dict[str, Any]]:
    url = f"https://data-api.polymarket.com/positions?{urlencode({'user': user})}"
    data = _get_json(url)
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _load_dotenv_map(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def prepare_r7a_artifacts(
    *,
    repo_root: Path,
    output_dir: Path,
    preflight_path: Path | None = None,
    issue_approval: bool = False,
    account_policy: str = "require_ack_resolved_redeemable",
) -> dict[str, Any]:
    """Build R7A.1 report. Never issues approval when ``issue_approval`` is False."""
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    trade = probe_btc_updown_market(now=now)
    commit = git_commit_identity(repo_root)

    # Recompute window/deadlines for report (probe already validated when unblocked)
    window_block = None
    deadlines_dict: dict[str, Any] | None = None
    try:
        events = _get_json(
            f"https://gamma-api.polymarket.com/events?slug={trade.market_slug}"
        )
        ev = events[0] if isinstance(events, list) and events else {}
        mkt = (ev.get("markets") or [None])[0] or {}
        window = resolve_btc_5m_window(
            slug=trade.market_slug, event=ev, market=mkt, now=now
        )
        deadlines = compute_lifecycle_deadlines(window, now=now)
        deadlines_dict = {
            "market_start": deadlines.market_start.isoformat(),
            "market_end": deadlines.market_end.isoformat(),
            "flatten_deadline": deadlines.flatten_deadline.isoformat(),
            "entry_deadline": deadlines.entry_deadline.isoformat(),
            "approval_expiration": deadlines.approval_expiration.isoformat(),
            "flatten_before_close_s": deadlines.flatten_before_close_s,
            "entry_safety_buffer_s": deadlines.entry_safety_buffer_s,
            "max_hold_s": deadlines.max_hold_s,
            "invariant": "approval_expiration ≤ entry_deadline < flatten_deadline < market_end",
        }
    except MarketWindowError as exc:
        window_block = str(exc)
    except Exception as exc:  # noqa: BLE001
        window_block = f"INVALID_MARKET_WINDOW:{type(exc).__name__}"

    preflight_hash = "none"
    user_stream_ready = False
    reconciliation_unreachable = True
    open_order_count = 0
    balance_ok = False
    if preflight_path and preflight_path.exists():
        raw = preflight_path.read_text(encoding="utf-8")
        preflight_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        pf = json.loads(raw)
        user_stream_ready = bool((pf.get("user_stream") or {}).get("authenticated"))
        recon = pf.get("reconciliation") or {}
        reconciliation_unreachable = bool(recon.get("unreachable_account", True))
        open_order_count = int(recon.get("open_order_count") or 0)
        balance_ok = bool((pf.get("balance_evidence") or {}).get("retrieved"))

    # Positions inventory (read-only)
    raw_positions: list[dict[str, Any]] = []
    inventory_error = None
    try:
        from tyrex_pm.execution.polymarket.auth import (
            load_l2_credentials,
            positions_wallet_address,
        )

        env_path = repo_root / ".env"
        if env_path.exists():
            creds = load_l2_credentials(_load_dotenv_map(env_path))
            user = positions_wallet_address(creds)
            raw_positions = _fetch_raw_positions(user)
    except Exception as exc:  # noqa: BLE001
        inventory_error = type(exc).__name__

    selected_tokens = [t for t in (trade.token_id, trade.opposite_token_id) if t]
    # Observation-only: venue positions with empty local store → not a truth conflict
    # for inventory classification; mark reconciliation_clean when account reachable
    # and no unknown external orders (open_order_count==0).
    recon_clean = (not reconciliation_unreachable) and open_order_count == 0
    inventory = build_inventory_report(
        raw_positions,
        selected_token_ids=selected_tokens,
        selected_condition_id=trade.condition_id or None,
        reconciliation_clean=recon_clean,
    )

    fee_resolved = trade.fee_rate is not None and trade.blocked != "FEE_PARAMETERS_UNKNOWN"
    sizing_blocker = None
    if trade.blocked and trade.blocked not in {
        "FEE_PARAMETERS_UNKNOWN",
        "MARKET_NOT_ACCEPTING_ORDERS",
        "INVALID_MARKET_WINDOW",
        "MARKET_DURATION_MISMATCH",
        "TITLE_TIME_MISMATCH",
        "ENTRY_DEADLINE_AFTER_FLATTEN",
        "INSUFFICIENT_TIME_REMAINING",
    }:
        sizing_blocker = trade.blocked
    market_blocker = window_block or (
        trade.blocked
        if trade.blocked
        in {
            "INVALID_MARKET_WINDOW",
            "MARKET_DURATION_MISMATCH",
            "TITLE_TIME_MISMATCH",
            "ENTRY_DEADLINE_AFTER_FLATTEN",
            "INSUFFICIENT_TIME_REMAINING",
            "MARKET_NOT_ACCEPTING_ORDERS",
        }
        else None
    )

    readiness = build_r7_readiness(
        inventory=inventory,
        user_stream_ready=user_stream_ready,
        reconciliation_unreachable=reconciliation_unreachable,
        open_order_count=open_order_count,
        fee_resolved=fee_resolved and trade.blocked != "FEE_PARAMETERS_UNKNOWN",
        market_window_blocker=market_blocker,
        sizing_blocker=sizing_blocker,
        balance_ok=balance_ok,
        account_policy=account_policy,
        mutations_enabled=False,
    )

    from tyrex_pm.execution.polymarket.signing_dry import validate_dry_signing_vector

    report: dict[str, Any] = {
        "phase": "R7A.1",
        "ts": now.isoformat(),
        "commit_identity": commit,
        "issue_approval": False,
        "approval_artifact_id": None,
        "mutations_enabled": False,
        "r7b_authorized": False,
        "dry_signing": validate_dry_signing_vector(),
        "field_provenance": [
            {
                "internal_field": p.internal_field,
                "api_field_source": p.api_field_source,
                "semantic_meaning": p.semantic_meaning,
                "trusted_for_scheduling": p.trusted_for_scheduling,
            }
            for p in PROVENANCE
        ],
        "market_time_correction": {
            "root_cause": (
                "R7A used Gamma event.startDate / market.startDate (listing time) "
                "as market_start. Authoritative start is slug epoch "
                "(cross-checked with eventStartTime/startTime); end = start+300s."
            ),
            "deadlines": deadlines_dict,
            "listed_at_not_schedule": trade.listed_at,
            "created_at_not_schedule": trade.created_at,
        },
        "heartbeat_decision": {
            "required_for_fak_oneshot": False,
            "will_call_in_r7b": False,
            "status": "avoided",
            "sources": [
                "https://docs.polymarket.com/api-reference/trade/send-heartbeat",
                "https://docs.polymarket.com/trading/orders/create",
            ],
        },
        "order_policy": {
            "recommended": "FAK",
            "buy_amount_unit": "USDC dollars to spend on shares",
            "quantity_semantics": "maximum_estimated_shares_not_guaranteed",
            "sources": ["https://docs.polymarket.com/trading/orders/create"],
        },
        "fees": {
            "source": "GET /clob-markets/{condition_id} fd + docs fee formula",
            "docs": "https://docs.polymarket.com/trading/fees",
            "fee_rate": trade.fee_rate,
            "exponent": trade.fee_exponent,
            "formula": "fee_usdc = shares * r * (p*(1-p))**e",
            "buy_amount_vs_fee": (
                "FAK BUY amount is USDC for shares; taker fee treated as additional "
                "USDC debit so amount + fee <= $5"
            ),
            "estimated_buy_fee": str(trade.estimated_buy_fee),
            "max_collateral": str(trade.max_collateral),
            "revised_max_fak_amount": str(trade.sized_amount),
        },
        "trade": {
            "market_title": trade.market_title,
            "market_slug": trade.market_slug,
            "condition_id_prefix": (
                trade.condition_id[:18] + "…" if trade.condition_id else ""
            ),
            "outcome_side": trade.outcome_side,
            "token_id_present": bool(trade.token_id),
            "tick_size": trade.tick_size,
            "min_order_size": trade.min_order_size,
            "best_ask": str(trade.best_ask),
            "ask_size": str(trade.ask_size),
            "sized_price": str(trade.sized_price),
            "max_estimated_shares": str(trade.sized_qty),
            "sized_amount_usd": str(trade.sized_amount),
            "estimated_buy_fee_usd": str(trade.estimated_buy_fee),
            "max_collateral_usd": str(trade.max_collateral),
            "order_type": trade.order_type,
            "market_start": trade.market_start,
            "market_end": trade.market_end,
            "accepting_orders": trade.accepting_orders,
            "probe_blocked": trade.blocked,
            "max_buy_notional_envelope": "5.00",
        },
        "deadlines": deadlines_dict,
        "position_inventory": inventory.to_dict(),
        "inventory_error": inventory_error,
        "reconciliation_policy": RECONCILIATION_POLICY_RECOMMENDATION,
        "exit_policy": EXIT_POLICY_V1,
        "r7_readiness": readiness.to_dict(),
        "preflight_hash": preflight_hash,
        "user_stream_ready": user_stream_ready,
        "worst_case": {
            "maximum_capital_at_risk": str(trade.max_collateral or Decimal("5.00")),
            "possible_spread_cost": "bounded_by_worst_price_equals_best_ask",
            "possible_entry_fee": str(trade.estimated_buy_fee),
            "possible_exit_fee": "fd_curve_at_exit_price_times_venue_shares",
            "possible_slippage_within_caps": "none_beyond_limit_price_on_entry",
            "residual_manual_intervention_risk": (
                "If bids disappear, loss may approach full acquisition cost "
                "(amount + entry fee), up to the $5 collateral envelope."
            ),
        },
    }

    # R7A.1: never issue approval artifact
    if issue_approval:
        report["approval_note"] = "issue_approval requested but R7A.1 forbids issuance"
    report["approval_artifact_id"] = None
    report["approval_blocked"] = readiness.blockers

    report_path = output_dir / "r7a_report.json"
    text = json.dumps(report, indent=2) + "\n"
    report["r7a_report_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    inv_path = output_dir / "r7a1_position_inventory.json"
    inv_path.write_text(
        json.dumps(inventory.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    return report
