"""Paper venue and account: the live runner's execution port with simulated money.

Composition (``venue: paper``): live public market data and the unchanged trading
runtime, lifecycle, coordinator, reconciler, account-state authority, final gate,
settlement ledger and resolution worker — with this module standing in for the
authenticated venue. Nothing here signs, holds credentials or talks to an
authenticated endpoint. One coherent simulated account backs every read the runtime
makes: collateral and token balances (and allowances), open orders, trade history,
order submission results and redemption credits.

Fill model (explicit assumptions, not a prediction of real fills):

* taker FAK only (``MarketBuyOrderSpec`` by spend amount up to ``worst_price``;
  ``MarketSellOrderSpec`` by shares down to ``minimum_price``);
* the order meets the *live* BookView captured after ``order_latency_ms``;
* each level contributes ``participation`` x displayed size; share quantities are
  rounded down to 0.01; liquidity is not depleted across separate orders;
* buy fees are charged in shares (net = gross x (1 - phi(p)/p)); sell fees in collateral;
* trades are reported CONFIRMED immediately (no matched/mined lag).

State is journaled to ``account_path`` (atomic JSON rewrite after every mutation), so a
paper run survives restarts exactly like a live account would.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tyrex_pm.domain.polymarket.fees import FeeCurveParams, phi_taker_fee_per_share
from tyrex_pm.execution.coordinator import GatewaySubmissionResult, PreparedOrder
from tyrex_pm.execution.orders import MarketBuyOrderSpec, MarketSellOrderSpec

PAPER_FILL_MODEL = "paper_live_book_taker_v1"
SHARE_QUANTUM = Decimal("0.01")


class PaperVenueError(RuntimeError):
    pass


def _floor(value: Decimal, step: Decimal = SHARE_QUANTUM) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass
class PaperAccount:
    """Simulated wallet. ``balances`` are outcome-token shares by token id."""

    path: Path | None
    collateral: Decimal = Decimal("0")
    balances: dict[str, Decimal] = field(default_factory=dict)
    token_condition: dict[str, str] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)
    redemptions: dict[str, dict] = field(default_factory=dict)
    order_serial: int = 0

    @classmethod
    def open(cls, path: Path | None, *, initial_collateral: Decimal) -> "PaperAccount":
        if path is not None and path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                path=path,
                collateral=Decimal(raw["collateral"]),
                balances={k: Decimal(v) for k, v in raw["balances"].items()},
                token_condition=dict(raw.get("token_condition", {})),
                trades=list(raw.get("trades", [])),
                redemptions=dict(raw.get("redemptions", {})),
                order_serial=int(raw.get("order_serial", 0)),
            )
        account = cls(path=path, collateral=initial_collateral)
        account.save()
        return account

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fill_model": PAPER_FILL_MODEL,
            "collateral": str(self.collateral),
            "balances": {k: str(v) for k, v in self.balances.items()},
            "token_condition": self.token_condition,
            "trades": self.trades,
            "redemptions": self.redemptions,
            "order_serial": self.order_serial,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


BookProvider = Callable[[], Any]  # () -> BookView | None
FeeProvider = Callable[[str], FeeCurveParams]


@dataclass
class PaperVenue:
    """Gateway port implemented against a ``PaperAccount`` and live public books."""

    account: PaperAccount
    fee_curve: FeeProvider
    book_provider: BookProvider | None = None
    order_latency_ms: int = 300
    participation: Decimal = Decimal("1")
    wallet: str = "paper:simulated"
    # Attributes the trading runtime assigns on real gateways; unused here.
    tick_size_resolver: Any = None
    tick_size_fetcher: Any = None
    client_guard: Any = None
    fill_context: Any = None
    evidence_sink: Any = None
    prepare_budget_ms: float = 0.0
    refresh_wait_budget_ms: float = 0.0
    mutations_enabled: bool = True
    posts: int = 0
    _stream_ready: Any = None

    # -- orders --------------------------------------------------------------------

    async def prepare_order(self, spec: Any) -> PreparedOrder:
        if not isinstance(spec, (MarketBuyOrderSpec, MarketSellOrderSpec)):
            raise PaperVenueError("paper venue supports taker FAK market orders only")
        return PreparedOrder(local_order_id=spec.order_id, digest=spec.order_id, payload=spec)

    async def discard_prepared(self, prepared: PreparedOrder) -> None:
        del prepared

    def _leg(self, token_id: str):
        view = None if self.book_provider is None else self.book_provider()
        if view is None:
            return None
        for leg in (view.up, view.down):
            if leg.token_id == token_id:
                return leg
        return None

    async def post_order(self, prepared: PreparedOrder) -> GatewaySubmissionResult:
        if not self.mutations_enabled:
            raise PaperVenueError("mutations disabled")
        spec = prepared.payload
        self.posts += 1
        if self.order_latency_ms:
            await asyncio.sleep(self.order_latency_ms / 1000)
        self.account.order_serial += 1
        venue_id = f"paper-order-{self.account.order_serial}"
        self.account.token_condition[spec.token_id] = spec.market_id
        leg = self._leg(spec.token_id)
        book = None if leg is None else leg.book
        curve = self.fee_curve(spec.token_id)
        fills: list[tuple[Decimal, Decimal]] = []
        if isinstance(spec, MarketBuyOrderSpec):
            if spec.spend_amount > self.account.collateral:
                self.account.save()
                return GatewaySubmissionResult(
                    accepted=False, venue_order_id=None, status="rejected",
                    error_code="not enough balance / allowance",
                )
            remaining = spec.spend_amount
            for level in sorted(() if book is None else book.asks, key=lambda lv: lv.price):
                if level.price > spec.worst_price or remaining <= 0:
                    break
                take = _floor(min(level.quantity * self.participation, remaining / level.price))
                if take <= 0:
                    continue
                fills.append((take, level.price))
                remaining -= take * level.price
        else:
            held = self.account.balances.get(spec.token_id, Decimal(0))
            remaining = min(spec.shares, held)
            for level in sorted(
                () if book is None else book.bids, key=lambda lv: lv.price, reverse=True
            ):
                if level.price < spec.minimum_price or remaining <= 0:
                    break
                take = _floor(min(level.quantity * self.participation, remaining))
                if take <= 0:
                    continue
                fills.append((take, level.price))
                remaining -= take
        trade_ids = []
        matched = Decimal(0)
        side = "BUY" if isinstance(spec, MarketBuyOrderSpec) else "SELL"
        now = datetime.now(timezone.utc)
        for shares, price in fills:
            fee_per_share = phi_taker_fee_per_share(price, curve)
            if side == "BUY":
                self.account.collateral -= shares * price
                net = shares * (Decimal(1) - fee_per_share / price)
                self.account.balances[spec.token_id] = (
                    self.account.balances.get(spec.token_id, Decimal(0)) + net
                )
                fee = shares * fee_per_share
            else:
                fee = shares * fee_per_share
                self.account.collateral += shares * price - fee
                self.account.balances[spec.token_id] = (
                    self.account.balances.get(spec.token_id, Decimal(0)) - shares
                )
            trade_id = f"paper-trade-{len(self.account.trades) + 1}"
            self.account.trades.append(
                {
                    "id": trade_id,
                    "taker_order_id": venue_id,
                    "status": "CONFIRMED",
                    "side": side,
                    "size": str(shares),
                    "price": str(price),
                    "fee": str(fee),
                    "asset_id": spec.token_id,
                    "market": spec.market_id,
                    "updated_at": now.isoformat(),
                }
            )
            trade_ids.append(trade_id)
            matched += shares
        self.account.save()
        return GatewaySubmissionResult(
            accepted=True,
            venue_order_id=venue_id,
            status="matched" if matched > 0 else "unmatched",
            cumulative_matched_shares=matched,
            trade_ids=tuple(trade_ids),
        )

    async def cancel_orders(self, order_ids):  # noqa: ANN001
        from tyrex_pm.execution.cancel import cancel_result_from_sdk

        return cancel_result_from_sdk(
            {"canceled": (), "not_canceled": {str(i): "not live" for i in order_ids}}
        )

    # -- reads ---------------------------------------------------------------------

    async def list_open_orders(self, **_kwargs: Any) -> tuple[Any, ...]:
        return ()

    async def list_account_trades(
        self, *, token_id: str | None = None, market_id: str | None = None
    ) -> tuple[Any, ...]:
        out = []
        for row in self.account.trades:
            if token_id and row["asset_id"] != token_id:
                continue
            if market_id and row["market"] != market_id:
                continue
            out.append(
                SimpleNamespace(
                    id=row["id"],
                    taker_order_id=row["taker_order_id"],
                    maker_orders=(),
                    status=row["status"],
                    side=row["side"],
                    size=Decimal(row["size"]),
                    price=Decimal(row["price"]),
                    asset_id=row["asset_id"],
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                )
            )
        return tuple(out)

    async def conditional_balance(self, token_id: str) -> tuple[Decimal, Decimal | None]:
        balance = self.account.balances.get(token_id, Decimal(0))
        return balance, balance

    async def collateral_balance(self) -> tuple[Decimal, Decimal | None]:
        return self.account.collateral, self.account.collateral

    async def warm_order_metadata(self, token_ids, **_kwargs):  # noqa: ANN001
        from tyrex_pm.execution.polymarket.gateway import (
            OrderMetadataWarmResult,
            OrderMetadataWarmTokenResult,
        )

        return OrderMetadataWarmResult(
            tokens=tuple(OrderMetadataWarmTokenResult(t, True, 0.0) for t in token_ids)
        )

    # -- user stream: trades are read back by reconciliation polling -----------------

    async def start_user_stream(self, *, session_id, markets, on_evidence, on_gap,  # noqa: ANN001
                                on_ready=None) -> None:
        del session_id, markets, on_evidence, on_gap
        if on_ready is not None:
            await on_ready()

    async def stop_user_stream(self) -> None:
        return None

    def startup_inspection(self, **_kwargs: Any):
        from tyrex_pm.execution.startup_reconciliation import (
            AccountIdentity,
            Holding,
            InspectionFacts,
            ReadCoverage,
        )

        holdings = tuple(
            Holding(condition_id=self.account.token_condition.get(token, ""), token_id=token,
                    shares=shares)
            for token, shares in self.account.balances.items()
            if shares > 0
        )
        return InspectionFacts(
            identity=AccountIdentity(chain_id=137, environment="paper", wallet=self.wallet),
            coverage=ReadCoverage(
                complete=True,
                positions_complete=True,
                orders_complete=True,
                activity_complete=True,
            ),
            holdings=holdings,
            closing_holdings=holdings,
            closing_open_orders=(),
            sessions=tuple(_kwargs.get("sessions") or ()),
        )

    async def close(self) -> None:
        self.account.save()

    # -- settlement (paper) ----------------------------------------------------------

    async def redeem(self, episode: Any) -> str | None:
        """Simulated operator redemption: burn the held shares, credit their payout."""
        key = episode.episode_id
        if key in self.account.redemptions:
            return self.account.redemptions[key]["tx"]
        if episode.payout_per_share is None:
            return None
        shares = min(episode.net_shares, self.account.balances.get(episode.token_id, Decimal(0)))
        credit = shares * episode.payout_per_share
        self.account.balances[episode.token_id] = (
            self.account.balances.get(episode.token_id, Decimal(0)) - shares
        )
        self.account.collateral += credit
        tx = f"paper-redeem:{key}"
        self.account.redemptions[key] = {"tx": tx, "credited": str(credit),
                                         "shares": str(shares)}
        self.account.save()
        return tx

    async def verify_redemption(self, tx_hash: str, episode: Any, wallet: str):
        from tyrex_pm.execution.settlement.resolution import RedemptionEvidence

        record = self.account.redemptions.get(episode.episode_id)
        ok = record is not None and record["tx"] == tx_hash and wallet == self.wallet
        return RedemptionEvidence(
            attributable=ok,
            finalized=ok,
            credited_amount=None if record is None else Decimal(record["credited"]),
            reasons=() if ok else ("PAPER_REDEMPTION_UNKNOWN",),
            tx_hash=tx_hash,
            source="paper_account",
        )


def delayed_paper_redeemer(venue: PaperVenue, delay_s: float):
    """Redeem a proven paper payout once it has waited ``delay_s`` in the worklist."""
    first_seen: dict[str, float] = {}
    loop_time = asyncio.get_event_loop

    async def redeem(episode: Any) -> str | None:
        now = loop_time().time()
        seen = first_seen.setdefault(episode.episode_id, now)
        if now - seen < delay_s:
            return None
        return await venue.redeem(episode)

    return redeem
