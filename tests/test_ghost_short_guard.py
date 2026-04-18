"""Ghost-short guard for ``apply_confirmed_trade_to_wallet``.

Background: prior to this guard, a SELL CONFIRMED arriving for a token with no recorded
long position created a negative-quantity ``WalletPosition`` with ``avg_price_usd=None``.
That single phantom row then caused **every** subsequent ``check_deployment_caps`` call to
short-circuit with ``DEPLOYMENT_MARK_UNKNOWN`` because the deployment evaluator iterates
all tokens in ``positions ∪ open_orders`` and treats any non-zero qty without a mark as
unpriceable. A few minutes of risk denials would follow until the bot was restarted.
"""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.state.shadow_wallet import apply_confirmed_trade_to_wallet
from tyrex_pm.state.wallet_store import WalletStore


def test_sell_confirmed_with_no_prior_long_drops_and_audits() -> None:
    w = WalletStore()
    t = TokenId("token-A")
    apply_confirmed_trade_to_wallet(
        w, token_id=t, side=Side.SELL, size=Decimal("10"), price=Decimal("0.5")
    )
    assert t not in w.positions, "ghost-short row must not be created"
    assert len(w.position_drift_audit) == 1
    entry = w.position_drift_audit[0]
    assert entry["kind"] == "sell_without_long"
    assert entry["token_id"] == "token-A"
    assert entry["size"] == "10"
    assert entry["trade_price"] == "0.5"


def test_buy_confirmed_with_no_prior_long_creates_position() -> None:
    """Symmetric BUY path is unaffected — long positions still come into being normally."""
    w = WalletStore()
    t = TokenId("token-B")
    apply_confirmed_trade_to_wallet(
        w, token_id=t, side=Side.BUY, size=Decimal("4"), price=Decimal("0.25")
    )
    assert w.positions[t].qty == Decimal("4")
    assert w.positions[t].avg_price_usd == Decimal("0.25")
    assert w.position_drift_audit == []


def test_sell_confirmed_oversized_against_existing_long_clamps_to_zero() -> None:
    """Mid-flight WS-vs-REST race: SELL > existing long must drop the row, not go negative."""
    w = WalletStore()
    t = TokenId("token-C")
    w.positions[t] = WalletPosition(
        token_id=t, qty=Decimal("3"), avg_price_usd=Decimal("0.5")
    )
    apply_confirmed_trade_to_wallet(
        w, token_id=t, side=Side.SELL, size=Decimal("10"), price=Decimal("0.6")
    )
    assert t not in w.positions, "oversized SELL must clamp to zero, not produce negative qty"
    assert len(w.position_drift_audit) == 1
    entry = w.position_drift_audit[0]
    assert entry["kind"] == "sell_oversized_existing_long"
    assert entry["prior_qty"] == "3"
    assert entry["sell_size"] == "10"


def test_normal_buy_then_partial_sell_stays_priced() -> None:
    """Sanity check: the happy path still produces a priced long after a partial sell."""
    w = WalletStore()
    t = TokenId("token-D")
    apply_confirmed_trade_to_wallet(
        w, token_id=t, side=Side.BUY, size=Decimal("10"), price=Decimal("0.4")
    )
    apply_confirmed_trade_to_wallet(
        w, token_id=t, side=Side.SELL, size=Decimal("6"), price=Decimal("0.55")
    )
    assert w.positions[t].qty == Decimal("4")
    assert w.positions[t].avg_price_usd == Decimal("0.4")  # avg unchanged on a sell-down
    assert w.position_drift_audit == []


def test_audit_ring_buffer_caps_at_max() -> None:
    """The audit list is capped so a runaway burst cannot grow memory unbounded."""
    w = WalletStore()
    t = TokenId("token-E")
    for i in range(250):
        apply_confirmed_trade_to_wallet(
            w, token_id=t, side=Side.SELL, size=Decimal("1"), price=Decimal("0.1")
        )
    assert len(w.position_drift_audit) == 200
    assert w.position_drift_audit[-1]["kind"] == "sell_without_long"
