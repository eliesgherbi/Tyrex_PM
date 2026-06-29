"""Paired binary strategy facade (Phase 4.6).

Entry evaluation builds BUY intents only; monitoring lives in
:class:`PairedBinaryMonitor`. This class exists for ``owner_id`` resolution,
exit submit callbacks from the pipeline, and optional generic ``on_signal``
compatibility (unused in the main loop).
"""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.models import EnterIntent, ExitIntent
from tyrex_pm.runtime.allocation_ids import PAIRED_BINARY_INTENT_SOURCE
from tyrex_pm.runtime.cashflows import (
    SOURCE_OMS_MATCH_EVIDENCE,
    extract_matched_cashflow,
)
from tyrex_pm.runtime.config import PairedBinaryStrategyConfig
from tyrex_pm.signals.base import Signal
from tyrex_pm.strategies.base import StrategyContext, StrategyResult
from tyrex_pm.strategies.paired_binary.entry_eval import EntryEvalInput, evaluate_entry, read_leg_book
from tyrex_pm.strategies.paired_binary.state import PairedBinaryRuntimeState


class PairedBinaryStrategy:
    def __init__(self, cfg: PairedBinaryStrategyConfig) -> None:
        self.cfg = cfg
        self._state: PairedBinaryRuntimeState | None = None
        self.last_exit_submitted_leg: str | None = None
        self.last_exit_blocked_leg: str | None = None
        self.last_exit_blocked_reason: str | None = None

    def bind_state(self, state: PairedBinaryRuntimeState) -> None:
        self._state = state
        self.last_exit_submitted_leg = None
        self.last_exit_blocked_leg = None
        self.last_exit_blocked_reason = None

    @property
    def owner_id(self) -> str:
        return self.cfg.owner_id

    def notify_exit_submitted(self, *, leg: str) -> None:
        self.last_exit_submitted_leg = leg

    def notify_exit_blocked(self, *, leg: str, reason: str) -> None:
        self.last_exit_blocked_leg = leg
        self.last_exit_blocked_reason = reason

    def notify_buy_submitted(
        self,
        ap,
        *,
        match_evidence=None,
        intent_extensions=None,
        apply_local_shadow_fill: bool = False,
    ) -> None:
        if self._state is None or not intent_extensions:
            return
        leg = str(intent_extensions.get("leg", ""))
        cid = str(ap.client_order_id)
        if leg == "yes":
            self._state.yes.entry_client_order_id = cid
            leg_rt = self._state.yes
        elif leg == "no":
            self._state.no.entry_client_order_id = cid
            leg_rt = self._state.no
        else:
            return
        intent = ap.intent
        if not isinstance(intent, EnterIntent) or intent.side != Side.BUY:
            return
        cf = extract_matched_cashflow(
            Side.BUY,
            match_evidence or {},
            source=SOURCE_OMS_MATCH_EVIDENCE,
            apply_shadow_fill=apply_local_shadow_fill,
            shadow_qty=intent.size,
            shadow_cash=intent.limit_price * intent.size if intent.limit_price else None,
        )
        if cf is None:
            return
        leg_rt.entry_cash = cf.cash
        leg_rt.entry_qty = cf.qty
        leg_rt.entry_cash_source = cf.source
        leg_rt.entry_vwap = cf.avg_price

    def notify_exit_matched(
        self,
        *,
        leg: str,
        match_evidence: dict | None,
        apply_local_shadow_fill: bool = False,
        size: Decimal | None = None,
        limit_price: Decimal | None = None,
    ) -> None:
        if self._state is None:
            return
        leg_rt = self._state.yes if leg == "yes" else self._state.no if leg == "no" else None
        if leg_rt is None:
            return
        cf = extract_matched_cashflow(
            Side.SELL,
            match_evidence or {},
            source=SOURCE_OMS_MATCH_EVIDENCE,
            apply_shadow_fill=apply_local_shadow_fill,
            shadow_qty=size,
            shadow_cash=limit_price * size if limit_price is not None and size is not None else None,
        )
        if cf is None:
            return
        leg_rt.exit_cash = cf.cash
        leg_rt.exit_qty = cf.qty
        leg_rt.exit_cash_source = cf.source
        leg_rt.exit_fill_price = cf.avg_price

    def on_signal(self, signal: Signal, ctx: StrategyContext) -> StrategyResult:
        return StrategyResult(intents=[], skip_reason="paired_binary_uses_direct_entry_eval")

    def evaluate_entry(
        self,
        ctx: StrategyContext,
        *,
        pair_correlation_id: str,
    ) -> tuple[list[tuple[EnterIntent, dict]], str | None]:
        """Return (intent, extensions) pairs and optional skip reason."""
        cfg = self.cfg
        ms = ctx.market_state
        from tyrex_pm.core.ids import TokenId

        yes = read_leg_book(ms, TokenId(cfg.yes_token_id), max_book_age_s=cfg.max_book_age_s)
        no = read_leg_book(ms, TokenId(cfg.no_token_id), max_book_age_s=cfg.max_book_age_s)

        result = evaluate_entry(
            EntryEvalInput(
                yes=yes,
                no=no,
                max_pair_entry_cost=cfg.max_pair_entry_cost,
                max_spread_yes=cfg.max_spread_yes,
                max_spread_no=cfg.max_spread_no,
                pair_stop_loss_pct=cfg.pair_stop_loss_pct,
                slippage_buffer=cfg.slippage_buffer,
                reject_if_spread_exceeds_loss_budget=cfg.reject_if_spread_exceeds_loss_budget,
            )
        )
        if not result.allowed:
            return [], result.reason

        qty = cfg.position_size
        yes_ask = yes.ask
        no_ask = no.ask
        assert yes_ask is not None and no_ask is not None

        yes_ext = {
            "source": PAIRED_BINARY_INTENT_SOURCE,
            "allocation_owner_id": cfg.owner_id,
            "pair_correlation_id": pair_correlation_id,
            "leg": "yes",
            "leg_correlation_id": f"{pair_correlation_id}:yes:entry",
        }
        no_ext = {
            "source": PAIRED_BINARY_INTENT_SOURCE,
            "allocation_owner_id": cfg.owner_id,
            "pair_correlation_id": pair_correlation_id,
            "leg": "no",
            "leg_correlation_id": f"{pair_correlation_id}:no:entry",
        }
        yes_intent = EnterIntent(
            token_id=TokenId(cfg.yes_token_id),
            side=Side.BUY,
            size=qty,
            limit_price=yes_ask,
            order_style=cfg.entry_order_style,
        )
        no_intent = EnterIntent(
            token_id=TokenId(cfg.no_token_id),
            side=Side.BUY,
            size=qty,
            limit_price=no_ask,
            order_style=cfg.entry_order_style,
        )
        return [(yes_intent, yes_ext), (no_intent, no_ext)], None
