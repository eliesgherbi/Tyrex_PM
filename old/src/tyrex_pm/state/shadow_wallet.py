from __future__ import annotations

import logging
from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent, ExitIntent, ReduceIntent, WalletPosition
from tyrex_pm.core.time import utc_now
from tyrex_pm.runtime.config import ShadowBootstrapConfig
from tyrex_pm.state.wallet_store import WalletStore

log = logging.getLogger(__name__)


def apply_shadow_bootstrap(wallet: WalletStore, cfg: ShadowBootstrapConfig) -> None:
    wallet.usdc_balance = cfg.usdc_balance
    wallet.usdc_allowance = cfg.usdc_allowance
    wallet.last_sync_ts = utc_now()


def apply_confirmed_trade_to_wallet(
    wallet: WalletStore,
    *,
    token_id: TokenId,
    side: Side,
    size: Decimal,
    price: Decimal,
) -> None:
    """Best-effort venue trade finality → position (user channel CONFIRMED)."""
    if side == Side.BUY:
        _add_qty(wallet, token_id, size, price)
    else:
        _add_qty(wallet, token_id, -size, price)


def apply_shadow_fill(wallet: WalletStore, ap: ApprovedIntent) -> None:
    """Update synthetic wallet state after a shadow execution (immediate fill model)."""
    intent = ap.intent
    if isinstance(intent, EnterIntent) and intent.side == Side.BUY:
        px = intent.limit_price or Decimal("0")
        cost = intent.size * px
        bal = wallet.usdc_balance or Decimal("0")
        wallet.usdc_balance = bal - cost
        _add_qty(wallet, intent.token_id, intent.size, px)
    elif isinstance(intent, (ExitIntent, ReduceIntent)) and intent.side == Side.SELL:
        px = intent.limit_price or Decimal("0")
        proceeds = intent.size * px
        bal = wallet.usdc_balance or Decimal("0")
        wallet.usdc_balance = bal + proceeds
        _add_qty(wallet, intent.token_id, -intent.size, px)


def _add_qty(wallet: WalletStore, token_id, dq: Decimal, trade_px: Decimal) -> None:
    cur = wallet.positions.get(token_id)
    if cur is None:
        if dq <= 0:
            # Ghost-short guard. A SELL CONFIRMED arrived for a token we have no recorded
            # long position for — typically because the matching BUY CONFIRMED was missed
            # (WS reconnect, or never received) or the trade originated from a manual UI
            # action before our wallet view was hydrated. Polymarket binary outcomes cannot
            # be shorted, and creating a negative-qty WalletPosition with avg_price_usd=None
            # taints every subsequent deployment-cap evaluation across the entire portfolio
            # via DEPLOYMENT_MARK_UNKNOWN. Drop and audit; the positions REST safety net
            # (data-api/positions) will repopulate the true long on its next tick.
            wallet.record_position_drift_audit(
                {
                    "ts_utc": utc_now().isoformat(),
                    "kind": "sell_without_long",
                    "token_id": str(token_id),
                    "size": str(abs(dq)),
                    "trade_price": str(trade_px) if trade_px is not None else None,
                    "note": "dropped_to_avoid_ghost_short_position",
                }
            )
            log.warning(
                "ghost-short guard: dropped SELL trade for token=%s size=%s with no recorded long",
                str(token_id),
                str(abs(dq)),
            )
            return
        wallet.positions[token_id] = WalletPosition(
            token_id=token_id,
            qty=dq,
            avg_price_usd=trade_px,
        )
        return
    new_q = cur.qty + dq
    if new_q == 0:
        wallet.positions.pop(token_id, None)
        return
    if new_q < 0:
        # Mirror the ghost-short guard above when an oversized SELL would push the position
        # negative. Clamp to zero, drop the row, and audit. The REST positions sync owns
        # ground truth; a mid-flight discrepancy must not poison the deployment evaluator.
        wallet.record_position_drift_audit(
            {
                "ts_utc": utc_now().isoformat(),
                "kind": "sell_oversized_existing_long",
                "token_id": str(token_id),
                "prior_qty": str(cur.qty),
                "sell_size": str(abs(dq)),
                "trade_price": str(trade_px) if trade_px is not None else None,
                "note": "clamped_to_zero_to_avoid_ghost_short_position",
            }
        )
        log.warning(
            "ghost-short guard: SELL would push token=%s below zero (prior=%s, sell=%s); clamping",
            str(token_id),
            str(cur.qty),
            str(abs(dq)),
        )
        wallet.positions.pop(token_id, None)
        return
    if dq > 0 and trade_px is not None:
        old_px = cur.avg_price_usd or trade_px
        old_v = old_px * cur.qty
        add_v = trade_px * dq
        avg = (old_v + add_v) / new_q
        wallet.positions[token_id] = WalletPosition(token_id=token_id, qty=new_q, avg_price_usd=avg)
    else:
        wallet.positions[token_id] = WalletPosition(
            token_id=token_id,
            qty=new_q,
            avg_price_usd=cur.avg_price_usd,
        )
