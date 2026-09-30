"""Normalized startup reads. SDK models stay behind this adapter.

Pinned ``polymarket-client`` 0.5.0 ``Market.resolution`` exposes UMA status and
no per-token payout vector. ``TradeActivity`` quantity is ``shares``, not
``size``. ``get_market`` accepts a Gamma id, slug, or URL, not a CLOB condition id.
Conditional ``get_balance_allowance`` balances are 6-decimal base units.
A standard-binary payout is read only when the caller supplies an ``eth_call``.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

SDK_FINALITY_LIMITATION = (
    "polymarket-client==0.5.0 Market.resolution has uma status and no payout "
    "vector; closed, end time, and redeemable are not finality"
)
WALLET_META_KEY = "account.wallet"
CHAIN_META_KEY = "account.chain_id"
ENVIRONMENT_META_KEY = "account.environment"
ASSOCIATION_META_KEY = "account.association"


@dataclass(frozen=True, kw_only=True)
class PageCoverage:
    complete: bool
    pages: int
    item_count: int
    reason: str = ""


def historical_market_query(*, condition_id: str) -> dict[str, tuple[str, ...]]:
    """Exact condition mapping. Never pass a condition id to ``get_market(id=)``.

    Gamma omits closed markets unless ``closed=True`` is also sent. The
    collector retries with that flag when the open query is empty.
    """
    if not str(condition_id).strip():
        raise ValueError("condition_id is required")
    return {"condition_ids": (condition_id,)}


async def _first_listed_market(client: Any, condition_id: str) -> Any:
    """Open query first, then closed markets. An empty open page is not UNKNOWN."""
    query = historical_market_query(condition_id=condition_id)
    market = await _first_page_item(client.list_markets(**query))
    if market is not None:
        return market
    return await _first_page_item(
        client.list_markets(**historical_market_query(condition_id=condition_id), closed=True)
    )


async def _first_page_item(source: Any) -> Any:
    async for item in source:
        page_items = getattr(item, "items", None)
        if page_items is None:
            return item
        return page_items[0] if page_items else None
    return None


def _outcome_slot(value: Any) -> int | None:
    if value in (0, 1):
        return int(value)
    if value is None or value == "":
        return None
    try:
        slot = int(value)
    except (TypeError, ValueError):
        return None
    return slot if slot in (0, 1) else None


async def _attach_closed_outcome_indexes(client: Any, holdings: list[Any]) -> list[Any]:
    """Copy a data-API outcome slot onto balance-only holdings. Never invent one."""
    if not hasattr(client, "list_closed_positions"):
        return holdings
    missing = {
        holding.condition_id
        for holding in holdings
        if holding.condition_id
        and holding.shares > 0
        and _outcome_slot(holding.outcome_index) is None
    }
    indexes: dict[tuple[str, str], int] = {}
    for condition_id in sorted(missing):
        try:
            rows, complete, _reason = await drain_paginator(
                client.list_closed_positions(market=(condition_id,))
            )
        except Exception:
            continue
        if not complete:
            continue
        for row in rows:
            token_id = str(_field(row, "token_id") or "")
            slot = _outcome_slot(_field(row, "outcome_index"))
            if token_id and slot is not None:
                indexes[(condition_id, token_id)] = slot
    updated = []
    for holding in holdings:
        slot = indexes.get((holding.condition_id, holding.token_id))
        if _outcome_slot(holding.outcome_index) is None and slot is not None:
            updated.append(replace(holding, outcome_index=slot))
        else:
            updated.append(holding)
    return updated


def position_list_kwargs() -> dict[str, float]:
    """Request a zero holding threshold so small positions are not omitted."""
    return {"size_threshold": 0.0}


def coverage_from_pages(
    *,
    pages_read: int,
    exhausted: bool,
    below_threshold_included: bool,
) -> PageCoverage:
    if pages_read < 0:
        raise ValueError("pages_read must be >= 0")
    complete = exhausted and below_threshold_included and pages_read >= 0
    reason = ""
    if not exhausted:
        reason = "pagination_truncated"
        complete = False
    elif not below_threshold_included:
        reason = "holding_threshold_not_zero"
        complete = False
    return PageCoverage(complete=complete, pages=pages_read, item_count=0, reason=reason)


def lifecycle_from_sdk_market(market: Mapping[str, Any] | Any) -> dict[str, str]:
    """Distinguish tradable and closed markets. Never invent a payout proof."""
    state = _field(market, "state")
    closed = bool(_field(state, "closed") if state is not None else _field(market, "closed"))
    if state is not None:
        accepting = _field(state, "accepting_orders")
    else:
        accepting = _field(market, "accepting_orders")
    resolution = _field(market, "resolution")
    uma = (
        _field(resolution, "uma_resolution_status")
        if resolution is not None
        else _field(market, "uma_resolution_status")
    )
    if accepting is True and not closed:
        status = "TRADABLE"
        reason = "market is accepting orders"
    elif closed or accepting is False:
        status = "CLOSED_PENDING_RESOLUTION"
        reason = SDK_FINALITY_LIMITATION
    else:
        status = "UNKNOWN"
        reason = SDK_FINALITY_LIMITATION
    return {
        "status": status,
        "proof_supported": "false",
        "reason": reason,
        "closed_observed": str(closed).lower(),
        "uma_resolution_status": "" if uma is None else str(getattr(uma, "value", uma)),
    }


def wallet_from_client(client: Any) -> str | None:
    """Position-owning wallet from the authenticated client, not the signer."""
    context = getattr(client, "_ctx", None)
    wallet = getattr(context, "wallet", None)
    if wallet:
        return str(wallet)
    wallet = getattr(client, "wallet", None)
    return None if not wallet else str(wallet)


def mask_wallet(wallet: str) -> str:
    text = str(wallet)
    if len(text) <= 10:
        return text[:2] + "…"
    return text[:6] + "…" + text[-4:]


def _field(value: Mapping[str, Any] | Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def shares_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        shares = Decimal(str(value))
    except Exception:
        return None
    if shares < 0:
        return None
    return shares


def merge_pages(pages: Sequence[Sequence[Any]]) -> tuple[tuple[Any, ...], bool]:
    """Concatenate pages. The caller must say whether the last page exhausted."""
    items: list[Any] = []
    for page in pages:
        items.extend(page)
    return tuple(items), True


def stored_wallet(journal: Any) -> str | None:
    if journal is None or not hasattr(journal, "get_meta"):
        return None
    value = journal.get_meta(WALLET_META_KEY)
    return None if not value else str(value)


def bind_unused_journal(journal: Any, wallet: str) -> None:
    """Record identity for a journal that has no sessions yet."""
    journal.set_meta(WALLET_META_KEY, wallet)
    journal.set_meta(CHAIN_META_KEY, "137")
    journal.set_meta(ENVIRONMENT_META_KEY, "production")


def associate_legacy_journal(journal: Any, wallet: str, *, at: datetime) -> None:
    """Operator association records provenance and does not clear exposure."""
    if stored_wallet(journal):
        raise ValueError("journal already has a wallet binding")
    journal.set_meta(WALLET_META_KEY, wallet)
    journal.set_meta(CHAIN_META_KEY, "137")
    journal.set_meta(ENVIRONMENT_META_KEY, "production")
    journal.set_meta(ASSOCIATION_META_KEY, "operator")
    journal.set_meta("account.associated_at", at.astimezone(timezone.utc).isoformat())


def _activity_observed_at(observed: Any) -> datetime | None:
    """Normalize an SDK timestamp. Integers and numeric strings are unix seconds."""
    if isinstance(observed, datetime):
        if observed.tzinfo is None:
            return observed.replace(tzinfo=timezone.utc)
        return observed
    if isinstance(observed, bool) or observed is None or observed == "":
        return None
    raw: int | None = None
    if isinstance(observed, (int, float)):
        raw = int(observed)
    elif isinstance(observed, str) and observed.isdigit():
        raw = int(observed)
    if raw is None or raw < 0:
        return None
    if raw > 10_000_000_000:
        raw //= 1000
    return datetime.fromtimestamp(raw, tz=timezone.utc)


def activity_from_sdk(item: Any) -> Any:
    """Normalize a pinned SDK activity. Quantity is ``shares``; hash is not a trade id."""
    from tyrex_pm.execution.startup_reconciliation import ActivityView

    side = _field(item, "side")
    activity_type = str(_field(item, "type") or "")
    shares = _field(item, "shares")
    if shares is None:
        shares = _field(item, "size")
    token_id = str(_field(item, "token_id") or _field(item, "asset") or "")
    condition_id = str(_field(item, "condition_id") or "")
    transaction_hash = _field(item, "transaction_hash")
    venue_trade_id = _field(item, "trade_id")
    if venue_trade_id is None:
        venue_trade_id = _field(item, "venue_trade_id")
    venue_order_id = _field(item, "order_id") or _field(item, "venue_order_id")
    observed = _activity_observed_at(_field(item, "timestamp"))
    activity_id = _field(item, "id")
    if not activity_id:
        activity_id = (
            f"{activity_type}:{condition_id}:{token_id}:{side}:{shares}:{transaction_hash}"
        )
    cause = activity_type.lower() or ("sale" if str(side).upper() == "SELL" else "activity")
    return ActivityView(
        activity_id=str(activity_id),
        condition_id=condition_id,
        token_id=token_id,
        shares=Decimal(str(shares or "0")),
        cause=cause,
        side=None if side is None else str(side),
        observed_at=observed if isinstance(observed, datetime) else None,
        transaction_hash=None if transaction_hash is None else str(transaction_hash),
        venue_trade_id=None if venue_trade_id is None else str(venue_trade_id),
        venue_order_id=None if venue_order_id is None else str(venue_order_id),
        activity_type=activity_type or None,
    )


def holding_from_sdk(item: Any) -> Any:
    from tyrex_pm.execution.startup_reconciliation import Holding

    shares = _field(item, "size")
    if shares is None:
        shares = _field(item, "shares")
    raw_index = _field(item, "outcome_index")
    if raw_index is None:
        raw_index = _field(item, "outcomeIndex")
    negative_risk = _field(item, "negative_risk")
    if negative_risk is None:
        negative_risk = _field(item, "neg_risk")
    outcome_index = None
    if raw_index is not None and raw_index != "":
        try:
            outcome_index = int(raw_index)
        except (TypeError, ValueError):
            outcome_index = None
    return Holding(
        condition_id=str(_field(item, "condition_id") or ""),
        token_id=str(_field(item, "token_id") or _field(item, "asset") or ""),
        shares=Decimal(str(shares or "0")),
        redeemable=_field(item, "redeemable"),
        outcome_index=outcome_index,
        negative_risk=None if negative_risk is None else bool(negative_risk),
    )


def open_order_from_sdk(item: Any) -> Any:
    from tyrex_pm.execution.startup_reconciliation import OpenOrderView

    return OpenOrderView(
        venue_order_id=str(_field(item, "id") or ""),
        condition_id=str(_field(item, "condition_id") or _field(item, "market") or ""),
        token_id=str(_field(item, "token_id") or _field(item, "asset_id") or ""),
        side=str(_field(item, "side") or ""),
        original_size=shares_or_none(
            _field(item, "original_size")
            if _field(item, "original_size") is not None
            else _field(item, "originalSize")
        ),
        size_matched=shares_or_none(
            _field(item, "size_matched")
            if _field(item, "size_matched") is not None
            else _field(item, "sizeMatched")
        ),
    )


async def drain_paginator(source: Any) -> tuple[list[Any], bool, str]:
    """Walk pages until ``has_more`` is false. Unverified iterators stay incomplete."""
    items, complete, reason, _pages = await drain_paginator_detailed(source)
    return items, complete, reason


async def drain_paginator_detailed(source: Any) -> tuple[list[Any], bool, str, int]:
    """Same walk as ``drain_paginator``, plus the number of pages observed."""
    items: list[Any] = []
    pages = 0
    if hasattr(source, "__aiter__"):
        saw_page = False
        try:
            async for page in source:
                saw_page = True
                pages += 1
                page_items = getattr(page, "items", None)
                if page_items is None:
                    items.append(page)
                    continue
                items.extend(page_items)
                if getattr(page, "has_more", False):
                    continue
                return items, True, "", pages
        except Exception as exc:  # truncated or malformed pagination is incomplete
            return items, False, f"{type(exc).__name__}:{exc}", pages
        if saw_page:
            return items, False, "pagination_truncated", pages
    if hasattr(source, "iter_items"):
        async for item in source.iter_items():
            items.append(item)
            pages += 1
        return items, False, "pagination_unverified", pages
    rows = list(source or ())
    return rows, True, "", 1 if rows else 0


async def _collect_activities(
    client: Any,
    *,
    journal: Any,
    wallet: str,
    chain_id: int,
    environment: str,
) -> tuple[tuple[Any, ...], bool, str, int, str, dict[str, Any] | None]:
    """Full or incremental activity. The returned ledger is only a proposal."""
    from tyrex_pm.execution.activity_checkpoint import (
        ACTIVITY_PAGE_SIZE,
        activities_from_ledger,
        collection_mode,
        incremental_start_unix,
        load_activity_ledger,
        propose_activity_ledger,
    )

    previous = load_activity_ledger(journal)
    mode = collection_mode(
        previous, wallet=wallet, chain_id=chain_id, environment=environment
    )
    start = incremental_start_unix(
        previous, wallet=wallet, chain_id=chain_id, environment=environment
    )
    kwargs: dict[str, Any] = {
        "page_size": ACTIVITY_PAGE_SIZE,
        "sort_by": "TIMESTAMP",
        "sort_direction": "ASC",
    }
    if start is not None:
        kwargs["start"] = start
    raw, complete, reason, pages = await drain_paginator_detailed(
        _call_accepting(client.list_activity, **kwargs)
    )
    fetched = tuple(activity_from_sdk(item) for item in raw)
    observed_unix = int(datetime.now(timezone.utc).timestamp())
    proposed = propose_activity_ledger(
        previous,
        fetched,
        mode=mode,
        complete=complete,
        wallet=wallet,
        chain_id=chain_id,
        environment=environment,
        observed_unix=observed_unix,
    )
    if proposed is None:
        retained = activities_from_ledger(previous)
        return retained, complete, reason, pages, mode, None
    return activities_from_ledger(proposed), complete, reason, pages, mode, proposed


def _incomplete(reason: str) -> Any:
    from tyrex_pm.execution.startup_reconciliation import InspectionFacts, ReadCoverage

    return InspectionFacts(
        identity=None,
        coverage=ReadCoverage(
            complete=False,
            positions_complete=False,
            orders_complete=False,
            activity_complete=False,
            reason=reason,
        ),
    )


NEG_RISK_FINALITY_GAP = (
    "neg-risk uses ConditionalTokens, but polymarket-client 0.5.0 does not prove "
    "the payoutNumerators slot for the held token or whether the condition is a "
    "named outcome, a placeholder, or Other"
)


def _is_neg_risk(market: Any) -> bool:
    state = _field(market, "state")
    flag = _field(state, "neg_risk") if state is not None else None
    if flag is None:
        flag = _field(market, "neg_risk")
    return bool(flag)


async def _conditional_shares(client: Any, token_id: str) -> tuple[Decimal | None, str]:
    """CLOB conditional balances are 6-decimal base units, not shares."""
    from tyrex_pm.adapters.polymarket.sdk_secure import balance_allowance_to_decimal

    if not hasattr(client, "get_balance_allowance"):
        return None, "conditional_balance_unavailable"
    raw = await client.get_balance_allowance(asset_type="CONDITIONAL", token_id=token_id)
    try:
        shares, _allowance = balance_allowance_to_decimal(raw, conditional=True)
    except ValueError as exc:
        return None, f"balance_units:{exc}"
    return shares, ""


async def _with_missing_balances(
    client: Any,
    holdings: list[Any],
    sessions: Sequence[Any],
    known_tokens: set[str],
) -> tuple[list[Any], str]:
    indexed = {holding.token_id for holding in holdings}
    reason = ""
    filled = list(holdings)
    for token_id in sorted(set(known_tokens) - indexed):
        shares, balance_reason = await _conditional_shares(client, token_id)
        if balance_reason:
            return filled, balance_reason
        if shares is None or shares <= 0:
            continue
        condition_id = ""
        for session in sessions:
            session_identity = getattr(session, "identity", None)
            if session_identity is not None and session_identity.token_id == token_id:
                condition_id = str(session_identity.market_id or "")
        filled.append(
            holding_from_sdk(
                {"condition_id": condition_id, "token_id": token_id, "size": shares}
            )
        )
    return filled, reason


async def _merge_ctf_payouts(
    lifecycles: dict[str, Any],
    holdings: Sequence[Any],
    neg_risk: set[str],
    eth_call: Any,
    settlement_cache: Any = None,
) -> dict[str, Any]:
    """Attach a standard-binary CTF payout when every held token is proven.

    ``settlement_cache`` is ignored. A ``latest`` observation is not reusable
    clearance evidence, so every inspection reads the payout again.
    """
    del settlement_cache
    from tyrex_pm.adapters.polymarket.ctf_payout import (
        CONDITIONAL_TOKENS_ADDRESS,
        read_binary_payout,
    )
    from tyrex_pm.execution.startup_reconciliation import MarketLifecycleObservation

    by_condition: dict[str, dict[str, Decimal]] = {}
    blocked: set[str] = set()
    cache: dict[tuple[str, int], Decimal | None] = {}
    for holding in holdings:
        condition_id = holding.condition_id
        if not condition_id or condition_id in neg_risk or holding.shares <= 0:
            continue
        current = lifecycles.get(condition_id)
        if current is None or current.status == "TRADABLE":
            continue
        index = holding.outcome_index
        if index not in (0, 1) or not holding.token_id:
            blocked.add(condition_id)
            continue
        key = (condition_id, index)
        if key not in cache:
            try:
                cache[key] = await read_binary_payout(
                    eth_call, condition_id=condition_id, outcome_index=index
                )
            except Exception:
                cache[key] = None
        if cache[key] is None:
            blocked.add(condition_id)
            continue
        by_condition.setdefault(condition_id, {})[holding.token_id] = cache[key]
    merged = dict(lifecycles)
    for condition_id, token_payouts in by_condition.items():
        if condition_id in blocked:
            continue
        held = {
            holding.token_id
            for holding in holdings
            if holding.condition_id == condition_id and holding.shares > 0
        }
        if held != set(token_payouts):
            continue
        single = next(iter(token_payouts.values())) if len(token_payouts) == 1 else None
        merged[condition_id] = MarketLifecycleObservation(
            condition_id=condition_id,
            status="RESOLVED_FINAL",
            payout_per_share=single,
            proof_supported=True,
            reason=(
                "read-only payoutNumerators/payoutDenominator on "
                f"{CONDITIONAL_TOKENS_ADDRESS}"
            ),
            token_payouts=dict(token_payouts),
        )
    return merged


def _call_accepting(method: Any, **kwargs: Any) -> Any:
    """Pass only parameters the installed client actually accepts."""
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return method(**kwargs)
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return method(**kwargs)
    names = set(signature.parameters)
    return method(**{key: value for key, value in kwargs.items() if key in names})


async def collect_inspection_facts(
    client: Any,
    *,
    journal: Any = None,
    sessions: Sequence[Any] = (),
    guard: Any = None,
    payout_eth_call: Any = None,
    settlement_cache: Any = None,
) -> Any:
    """Read account facts on the caller's client. Never takes the execution SDK slot.

    ``guard`` is accepted and ignored. Inspection must not serialize behind
    order preparation.
    """
    del guard
    started = time.monotonic_ns()
    facts = await _collect_inspection_facts(
        client,
        journal=journal,
        sessions=sessions,
        payout_eth_call=payout_eth_call,
        settlement_cache=settlement_cache,
    )
    elapsed_ms = round((time.monotonic_ns() - started) / 1_000_000, 3)
    return replace(facts, inspection_duration_ms=elapsed_ms)


async def _collect_inspection_facts(
    client: Any,
    *,
    journal: Any,
    sessions: Sequence[Any],
    payout_eth_call: Any = None,
    settlement_cache: Any = None,
) -> Any:
    from tyrex_pm.execution.startup_reconciliation import (
        AccountIdentity,
        InspectionFacts,
        MarketLifecycleObservation,
        OrderLookup,
        ReadCoverage,
    )

    wallet = wallet_from_client(client)
    if not wallet:
        return _incomplete("authenticated wallet is not visible")
    identity = AccountIdentity(chain_id=137, environment="production", wallet=str(wallet))
    try:
        positions, positions_complete, position_reason = await drain_paginator(
            client.list_positions(**position_list_kwargs())
        )
        holdings = [holding_from_sdk(item) for item in positions]
        orders, orders_complete, order_reason = await drain_paginator(client.list_open_orders())
        open_orders = tuple(open_order_from_sdk(item) for item in orders)
        known_tokens = {holding.token_id for holding in holdings if holding.token_id}
        known_tokens.update(order.token_id for order in open_orders if order.token_id)
        for session in sessions:
            session_identity = getattr(session, "identity", None)
            token_id = None if session_identity is None else session_identity.token_id
            if token_id:
                known_tokens.add(token_id)
        holdings, missing_reason = await _with_missing_balances(
            client, holdings, sessions, known_tokens
        )
        holdings = await _attach_closed_outcome_indexes(client, holdings)
        (
            activities,
            activity_complete,
            activity_reason,
            activity_pages,
            activity_mode,
            activity_ledger,
        ) = await _collect_activities(
            client,
            journal=journal,
            wallet=str(wallet),
            chain_id=identity.chain_id,
            environment=identity.environment,
        )
        lookups: dict[str, OrderLookup] = {}
        for session in sessions:
            for order in getattr(session, "orders", {}).values():
                venue_order_id = getattr(order, "venue_order_id", None)
                if not venue_order_id or not getattr(order, "ambiguous", False):
                    continue
                try:
                    found = await client.get_order(order_id=str(venue_order_id))
                except Exception as exc:  # missing proof stays unresolved
                    lookups[str(venue_order_id)] = OrderLookup(
                        found=False,
                        terminal=False,
                        matched_shares=Decimal("0"),
                        status=type(exc).__name__,
                        not_found=True,
                    )
                    continue
                status = str(_field(found, "status") or "")
                matched = Decimal(str(_field(found, "size_matched") or "0"))
                lookups[str(venue_order_id)] = OrderLookup(
                    found=True,
                    terminal=status.lower() in {"canceled", "cancelled", "matched", "unmatched"},
                    matched_shares=matched,
                    status=status,
                )
        lifecycles = {}
        neg_risk: set[str] = set()
        condition_ids = {holding.condition_id for holding in holdings if holding.condition_id}
        for session in sessions:
            session_identity = getattr(session, "identity", None)
            if session_identity is not None and session_identity.market_id:
                condition_ids.add(session_identity.market_id)
        for condition_id in condition_ids:
            market = await _first_listed_market(client, condition_id)
            if market is not None and _is_neg_risk(market):
                neg_risk.add(condition_id)
                lifecycles[condition_id] = MarketLifecycleObservation(
                    condition_id=condition_id,
                    status="UNKNOWN",
                    proof_supported=False,
                    reason=NEG_RISK_FINALITY_GAP,
                )
                continue
            observed = lifecycle_from_sdk_market({} if market is None else market)
            lifecycles[condition_id] = MarketLifecycleObservation(
                condition_id=condition_id,
                status=observed["status"],
                proof_supported=False,
                reason=observed["reason"],
            )
        for holding in holdings:
            if holding.negative_risk and holding.condition_id:
                neg_risk.add(holding.condition_id)
                current = lifecycles.get(holding.condition_id)
                if current is not None and current.status != "TRADABLE":
                    lifecycles[holding.condition_id] = MarketLifecycleObservation(
                        condition_id=holding.condition_id,
                        status="UNKNOWN",
                        proof_supported=False,
                        reason=NEG_RISK_FINALITY_GAP,
                    )
        if payout_eth_call is not None:
            lifecycles = await _merge_ctf_payouts(
                lifecycles,
                holdings,
                neg_risk,
                payout_eth_call,
                settlement_cache,
            )
        closing_raw, closing_complete, _closing_reason = await drain_paginator(
            client.list_positions(**position_list_kwargs())
        )
        closing, closing_balance_reason = await _with_missing_balances(
            client,
            [holding_from_sdk(item) for item in closing_raw],
            sessions,
            known_tokens,
        )
        if closing_balance_reason and not missing_reason:
            missing_reason = closing_balance_reason
        closing_order_rows, closing_orders_complete, _order_close_reason = await drain_paginator(
            client.list_open_orders()
        )
        closing_orders = tuple(open_order_from_sdk(item) for item in closing_order_rows)
    except Exception as exc:
        return InspectionFacts(
            identity=identity,
            coverage=ReadCoverage(
                complete=False,
                positions_complete=False,
                orders_complete=False,
                activity_complete=False,
                reason=f"{type(exc).__name__}:{exc}",
            ),
        )
    stored = stored_wallet(journal)
    legacy = False
    bound = stored
    if stored is None and sessions:
        legacy = True
        bound = None
    elif stored is None and journal is not None and not sessions:
        bind_unused_journal(journal, str(wallet))
        bound = str(wallet)
    reasons = [
        reason
        for reason in (
            position_reason,
            order_reason,
            activity_reason,
            missing_reason,
        )
        if reason
    ]
    complete = (
        positions_complete
        and orders_complete
        and activity_complete
        and closing_complete
        and closing_orders_complete
        and not missing_reason
    )
    return InspectionFacts(
        identity=identity,
        coverage=ReadCoverage(
            complete=complete,
            positions_complete=positions_complete,
            orders_complete=orders_complete and closing_orders_complete,
            activity_complete=activity_complete,
            known_tokens_checked=tuple(sorted(known_tokens)),
            reason=";".join(reasons),
        ),
        holdings=tuple(holdings),
        open_orders=open_orders,
        activities=activities,
        order_lookups=lookups,
        lifecycles=lifecycles,
        bound_wallet=bound,
        legacy_unbound=legacy,
        closing_holdings=tuple(closing),
        closing_open_orders=closing_orders,
        activity_mode=activity_mode,
        activity_pages=activity_pages,
        activity_ledger=activity_ledger,
    )
