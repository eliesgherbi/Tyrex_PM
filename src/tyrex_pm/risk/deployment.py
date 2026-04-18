"""
LOCKED deployment accounting for copy-strategy parity (do not reinterpret).

All USD amounts use Decimal. Marks come from RiskContext.mark_prices.

**Per-token deployed USD** for token T:

  (1) **Position leg**
      Let q = signed position qty (outcome tokens; positive = long the outcome).
      Let mark = mark_prices.get(T) or position.avg_price_usd.
      If mark is None and q != 0: token position value is **unknown** → deny with
      `DEPLOYMENT_MARK_UNKNOWN`.

      position_usd = abs(q) * mark

  (2) **Open BUY reserves**
      Sum over all open orders o where o.token_id == T and o.side == BUY:
          reserved_usd += o.remaining_size * o.limit_price

  (3) **Hypothetical new BUY (risk preview)**
      When evaluating an `EnterIntent` with side BUY, include the same reserve as
      if that order were already resting, using `limit_price` if set else
      `mark_prices[T]`. If both are missing → `DEPLOYMENT_MARK_UNKNOWN`.

  (4) **Open SELL orders**
      Do **not** add to deployment USD (they reduce inventory; inventory gate handles SELL).

  deployed_usd(T) = position_usd + reserved_usd

**Portfolio deployed USD** = sum_T deployed_usd(T)

Caps: token_cap_usd and portfolio_cap_usd compared using standard <=.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import EnterIntent, ExitIntent, OpenOrderView, ReduceIntent, RiskContext, WalletPosition
from tyrex_pm.risk.evidence_format import s_usd, s_usd_map


@dataclass(frozen=True)
class RiskConfigCaps:
    token_cap_usd: Decimal
    portfolio_cap_usd: Decimal


def position_value_usd(pos: WalletPosition, mark: Decimal | None) -> tuple[Decimal | None, bool]:
    """
    Returns (value_usd, unknown) — unknown True if we cannot price non-zero qty.
    """
    if pos.qty == 0:
        return Decimal("0"), False
    px = mark if mark is not None else pos.avg_price_usd
    if px is None:
        return None, True
    return abs(pos.qty) * px, False


def open_buy_reserved_usd(orders: tuple[OpenOrderView, ...], token_id: TokenId) -> Decimal:
    s = Decimal("0")
    for o in orders:
        if o.token_id == token_id and o.side == Side.BUY:
            s += o.remaining_size * o.limit_price
    return s


def deployed_usd_for_token(
    *,
    token_id: TokenId,
    positions: dict[TokenId, WalletPosition],
    open_orders: tuple[OpenOrderView, ...],
    mark_prices: dict[TokenId, Decimal],
) -> tuple[Decimal | None, bool]:
    """Returns (deployed_usd, unknown_mark)."""
    pos = positions.get(token_id)
    if pos is None or pos.qty == 0:
        pos_val, unk = Decimal("0"), False
    else:
        pos_val, unk = position_value_usd(pos, mark_prices.get(token_id))
        if unk:
            return None, True
    assert pos_val is not None
    reserved = open_buy_reserved_usd(open_orders, token_id)
    return pos_val + reserved, False


def portfolio_deployed_usd(
    *,
    positions: dict[TokenId, WalletPosition],
    open_orders: tuple[OpenOrderView, ...],
    mark_prices: dict[TokenId, Decimal],
) -> tuple[Decimal | None, bool]:
    tokens: set[TokenId] = set(positions.keys())
    for o in open_orders:
        tokens.add(o.token_id)
    total = Decimal("0")
    for t in tokens:
        d, unk = deployed_usd_for_token(
            token_id=t,
            positions=positions,
            open_orders=open_orders,
            mark_prices=mark_prices,
        )
        if unk:
            return None, True
        assert d is not None
        total += d
    return total, False


def _synthetic_open_buy_for_intent(
    intent: EnterIntent | ExitIntent | ReduceIntent,
    mark_prices: dict[TokenId, Decimal],
) -> OpenOrderView | None | str:
    """
    Returns synthetic resting BUY for deployment preview, None if not applicable,
    or 'unknown' if a BUY intent cannot be priced.
    """
    if not isinstance(intent, EnterIntent) or intent.side != Side.BUY:
        return None
    px = intent.limit_price
    if px is None:
        px = mark_prices.get(intent.token_id)
    if px is None:
        return "unknown"
    return OpenOrderView(
        token_id=intent.token_id,
        side=Side.BUY,
        remaining_size=intent.size,
        limit_price=px,
        client_order_id=None,
        venue_order_id=None,
    )


def open_orders_with_pending_intent(
    risk_ctx: RiskContext,
    pending_intent: EnterIntent | ExitIntent | ReduceIntent | None,
) -> tuple[tuple[OpenOrderView, ...] | None, str | None]:
    """Returns (open_orders, error_reason). error_reason set when pricing fails.

    The returned tuple includes:

    * ``risk_ctx.open_orders`` — venue-truth open orders (WS+REST merged in WalletStore).
    * ``risk_ctx.in_flight_buy_reservations`` — synthetic resting BUYs for provisional
      ``LocalOrder`` rows the venue has accepted (or about to) but ``open_orders`` has not
      yet mirrored. Always dedup'd against ``open_orders`` by ``venue_order_id`` upstream
      (see :func:`tyrex_pm.risk.in_flight.derive_in_flight_buy_reservations`).
    * The synthetic resting BUY for ``pending_intent`` (when applicable).

    Without the in-flight reservation leg, deployment + capital approve orders against
    capital the venue has already locked (HTTP 400 ``not enough balance / allowance``).
    """
    orders: tuple[OpenOrderView, ...] = risk_ctx.open_orders + tuple(
        risk_ctx.in_flight_buy_reservations
    )
    if pending_intent is None:
        return orders, None
    syn = _synthetic_open_buy_for_intent(pending_intent, risk_ctx.mark_prices)
    if syn == "unknown":
        return None, rc.DEPLOYMENT_MARK_UNKNOWN
    if isinstance(syn, OpenOrderView):
        return orders + (syn,), None
    return orders, None


def evaluate_deployment_caps(
    caps: RiskConfigCaps,
    risk_ctx: RiskContext,
    *,
    pending_intent: EnterIntent | ExitIntent | ReduceIntent | None = None,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Evaluate token + portfolio deployment caps and return diagnostic evidence.

    Returns ``(ok, reason_code_or_none, evidence)``. ``evidence`` is always populated and
    is intended to be merged into the ``risk_decision`` fact ``extensions`` so operators
    can answer "why did portfolio_deployment_cap deny this intent?" without re-deriving
    the inputs from other facts. The dict contains:

    * ``per_token_deployed_usd``: ``{token_id_str: str_decimal}`` for every token examined,
      capturing the position leg + open BUY reserve + (when applicable) the synthetic
      resting BUY for the pending intent.
    * ``portfolio_deployed_usd``: total deployed across tokens (only when no
      mark-unknown short-circuit fired).
    * ``token_cap_usd`` / ``portfolio_cap_usd``: cap configuration for context.
    * ``denied_token_id``: set on per-token cap breach.
    * ``marks_present`` / ``marks_missing``: token-id sets describing which tokens had
      a usable mark from ``RiskContext.mark_prices`` ∪ ``WalletPosition.avg_price_usd``.
    * ``synthetic_buy_added``: ``True`` when a pending BUY intent contributed a
      synthetic reservation row.
    """
    evidence: dict[str, Any] = {
        "token_cap_usd": s_usd(caps.token_cap_usd),
        "portfolio_cap_usd": s_usd(caps.portfolio_cap_usd),
    }

    open_orders, err = open_orders_with_pending_intent(risk_ctx, pending_intent)
    # Surface the in-flight reservation leg unconditionally so operators can answer
    # "was the cap aware of recently submitted orders the wallet view hadn't yet mirrored?"
    # for *every* risk_decision (approve and deny), not just deny-with-evidence paths.
    in_flight = tuple(risk_ctx.in_flight_buy_reservations)
    in_flight_total = Decimal("0")
    in_flight_by_token_dec: dict[str, Decimal] = {}
    for r in in_flight:
        usd = r.remaining_size * r.limit_price
        in_flight_total += usd
        in_flight_by_token_dec[str(r.token_id)] = (
            in_flight_by_token_dec.get(str(r.token_id), Decimal("0")) + usd
        )
    evidence["in_flight_reserved_usd_total"] = s_usd(in_flight_total)
    evidence["in_flight_reserved_usd_by_token"] = s_usd_map(in_flight_by_token_dec)
    evidence["in_flight_reservation_count"] = len(in_flight)
    if err:
        evidence["mark_unknown_for_pending_intent"] = True
        return False, err, evidence
    assert open_orders is not None
    # ``synthetic_buy_added`` is True iff the pending intent contributed a row OR the
    # in-flight reservation leg added rows beyond plain wallet truth. Both expand the
    # set used by deployment math and are interesting for post-mortem.
    evidence["synthetic_buy_added"] = (
        open_orders is not risk_ctx.open_orders
    )

    positions = {p.token_id: p for p in risk_ctx.wallet_positions}
    tokens: set[TokenId] = set(positions.keys())
    for o in open_orders:
        tokens.add(o.token_id)

    per_token_dec: dict[str, Decimal] = {}
    marks_present: list[str] = []
    marks_missing: list[str] = []
    for token_id in tokens:
        pos = positions.get(token_id)
        has_mark = (token_id in risk_ctx.mark_prices) or (
            pos is not None and pos.avg_price_usd is not None
        )
        if has_mark or pos is None or pos.qty == 0:
            marks_present.append(str(token_id))
        else:
            marks_missing.append(str(token_id))

    evidence["marks_present"] = sorted(marks_present)
    evidence["marks_missing"] = sorted(marks_missing)

    for token_id in sorted(tokens, key=str):
        d, unk = deployed_usd_for_token(
            token_id=token_id,
            positions=positions,
            open_orders=open_orders,
            mark_prices=risk_ctx.mark_prices,
        )
        if unk:
            evidence["per_token_deployed_usd"] = s_usd_map(per_token_dec)
            evidence["mark_unknown_token_id"] = str(token_id)
            return False, rc.DEPLOYMENT_MARK_UNKNOWN, evidence
        assert d is not None
        per_token_dec[str(token_id)] = d
        if d > caps.token_cap_usd:
            evidence["per_token_deployed_usd"] = s_usd_map(per_token_dec)
            evidence["denied_token_id"] = str(token_id)
            return False, rc.TOKEN_DEPLOYMENT_CAP, evidence

    evidence["per_token_deployed_usd"] = s_usd_map(per_token_dec)

    ptot, unk2 = portfolio_deployed_usd(
        positions=positions,
        open_orders=open_orders,
        mark_prices=risk_ctx.mark_prices,
    )
    if unk2:
        return False, rc.DEPLOYMENT_MARK_UNKNOWN, evidence
    assert ptot is not None
    evidence["portfolio_deployed_usd"] = s_usd(ptot)
    if ptot > caps.portfolio_cap_usd:
        return False, rc.PORTFOLIO_DEPLOYMENT_CAP, evidence
    return True, None, evidence


def check_deployment_caps(
    caps: RiskConfigCaps,
    risk_ctx: RiskContext,
    *,
    pending_intent: EnterIntent | ExitIntent | ReduceIntent | None = None,
) -> tuple[bool, str | None]:
    """Back-compat wrapper around :func:`evaluate_deployment_caps` (drops evidence)."""
    ok, reason, _ = evaluate_deployment_caps(caps, risk_ctx, pending_intent=pending_intent)
    return ok, reason
