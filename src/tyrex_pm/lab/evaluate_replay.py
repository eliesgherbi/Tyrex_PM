"""Deterministic evaluate() replay from fixtures — no gateway, no --live."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import (
    InstrumentId,
    MarketId,
    RunId,
    TokenId,
    new_correlation_id,
    new_event_id,
)
from tyrex_pm.core.instruments import Instrument, OutcomeSide
from tyrex_pm.core.snapshots import ReferencePriceSnapshot
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.facts import ids as F
from tyrex_pm.indicators.depth import DepthLevel, DepthSnapshot
from tyrex_pm.indicators.graph import IndicatorGraph
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import ExecutableQuote
from tyrex_pm.market_data.freshness import FreshnessAssessment, FreshnessReason, TimestampBasis
from tyrex_pm.strategies.context import DecisionContext, StrategyContext
from tyrex_pm.strategies.evaluation import StrategyEvaluation
from tyrex_pm.strategies.registry import StrategyPlugin, plugin_indicator_specs

LAB_SCHEMA = "lab_evaluate_v1"


@dataclass(frozen=True, kw_only=True)
class ReplayBooks:
    up_ask: Decimal | None = None
    down_ask: Decimal | None = None
    up_bid: Decimal | None = None
    down_bid: Decimal | None = None


@dataclass(frozen=True, kw_only=True)
class ReplayTrade:
    source: str
    price: Decimal


@dataclass(frozen=True, kw_only=True)
class ReplayStep:
    ts: datetime
    evaluate: bool = False
    books: ReplayBooks | None = None
    depth: DepthSnapshot | None = None
    depth_source: str | None = None
    trade: ReplayTrade | None = None
    reference_price: Decimal | None = None


@dataclass(frozen=True, kw_only=True)
class LabPack:
    market: BinaryMarket
    steps: tuple[ReplayStep, ...]
    target_notional: Decimal = Decimal("5")
    window_id: str = "lab"


def evaluate_replay(
    *,
    plugin: StrategyPlugin,
    config: Any,
    steps: Sequence[ReplayStep],
    market: BinaryMarket,
    target_notional: Decimal = Decimal("5"),
    window_id: str = "lab",
    entry_allowed: bool = True,
) -> tuple[StrategyEvaluation, ...]:
    """Run plugin.evaluate() on fixture steps. Never imports the venue gateway."""
    if not steps:
        return ()
    clock = FakeClock(_wall=steps[0].ts)
    driver = plugin.create_driver(
        config=config,
        clock=clock,
        target_notional=target_notional,
        window_id=window_id,
    )
    driver.on_start(
        StrategyContext(run_id=RunId("lab"), strategy_id=driver.strategy_id, market=market)
    )
    graph = IndicatorGraph(plugin_indicator_specs(plugin, config))
    last_books = ReplayBooks()
    last_reference: Decimal | None = None
    results: list[StrategyEvaluation] = []
    for step in steps:
        clock.set_utc(step.ts)
        if step.books is not None:
            last_books = step.books
        if step.reference_price is not None:
            last_reference = step.reference_price
        if step.trade is not None:
            graph.on_trade(
                source=step.trade.source,
                price=step.trade.price,
                ts_event=step.ts,
            )
        if step.depth is not None:
            graph.on_depth(step.depth, source=step.depth_source or F.BINANCE_SPOT_L2)
        if not step.evaluate:
            continue
        snapshot = _snapshot(
            market=market,
            books=last_books,
            ts=step.ts,
            reference_price=last_reference,
        )
        context = DecisionContext(
            run_id=RunId("lab"),
            snapshot=snapshot,
            target_notional=target_notional,
            now=step.ts,
            entry_allowed=entry_allowed,
        )
        results.append(
            driver.evaluate(
                market_snapshot=snapshot,
                causation_id=snapshot.causation_id,
                correlation_id=snapshot.correlation_id,
                context=context,
                trigger="feed",
                features=graph.snapshot(),
            )
        )
    return tuple(results)


def load_lab_pack(path: Path | str) -> LabPack:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("lab fixture must be a JSON object")
    if raw.get("schema") != LAB_SCHEMA:
        raise ValueError(f"lab fixture schema must be {LAB_SCHEMA}")
    window = dict(raw.get("window") or {})
    market = _market_from_window(window)
    steps = tuple(_step_from_mapping(item) for item in raw.get("steps") or ())
    if not steps:
        raise ValueError("lab fixture steps must be non-empty")
    notional = Decimal(str(raw.get("target_notional", "5")))
    window_id = str(window.get("window_id") or "lab")
    return LabPack(
        market=market,
        steps=steps,
        target_notional=notional,
        window_id=window_id,
    )


def replay_configured_run(
    *,
    config_path: Path | str,
    fixture_path: Path | str,
) -> tuple[StrategyEvaluation, ...]:
    from tyrex_pm.runtime.run_config import load_trading_run_config
    from tyrex_pm.strategies.registry import get_strategy_plugin

    run = load_trading_run_config(Path(config_path))
    plugin = get_strategy_plugin(run.strategy_kind)
    pack = load_lab_pack(fixture_path)
    return evaluate_replay(
        plugin=plugin,
        config=run.strategy,
        steps=pack.steps,
        market=pack.market,
        target_notional=pack.target_notional or run.risk.target_notional,
        window_id=pack.window_id,
    )


def evaluation_as_record(result: StrategyEvaluation) -> dict[str, Any]:
    return {
        "action": result.decision.action.value,
        "reason_code": result.decision.reason_code,
        "strategy_inputs_eligible": result.eligibility.strategy_inputs_eligible,
        "model_ready": result.eligibility.model_ready,
        "blockers": list(result.eligibility.blockers),
        "intent_count": len(result.intents),
        "intent_kinds": [intent.kind.value for intent in result.intents],
        "evidence": dict(result.decision.evidence),
    }


def _step_from_mapping(raw: Any) -> ReplayStep:
    if not isinstance(raw, dict):
        raise ValueError("each lab step must be an object")
    ts = _parse_ts(raw.get("ts"))
    books = None
    if raw.get("books") is not None:
        book_raw = dict(raw["books"])
        books = ReplayBooks(
            up_ask=_optional_decimal(book_raw.get("up_ask")),
            down_ask=_optional_decimal(book_raw.get("down_ask")),
            up_bid=_optional_decimal(book_raw.get("up_bid")),
            down_bid=_optional_decimal(book_raw.get("down_bid")),
        )
    depth = None
    depth_source = None
    if raw.get("depth") is not None:
        depth_raw = dict(raw["depth"])
        depth_source = str(depth_raw.get("source") or F.BINANCE_SPOT_L2)
        depth = DepthSnapshot(
            instrument_id=InstrumentId(str(depth_raw.get("instrument_id") or "btcusdt")),
            ts_event=ts,
            bids=_levels(depth_raw.get("bids") or ()),
            asks=_levels(depth_raw.get("asks") or ()),
        )
    trade = None
    if raw.get("trade") is not None:
        trade_raw = dict(raw["trade"])
        trade = ReplayTrade(
            source=str(trade_raw.get("source") or F.BINANCE_SPOT_TRADES),
            price=Decimal(str(trade_raw["price"])),
        )
    return ReplayStep(
        ts=ts,
        evaluate=bool(raw.get("evaluate", False)),
        books=books,
        depth=depth,
        depth_source=depth_source,
        trade=trade,
        reference_price=_optional_decimal(raw.get("reference_price")),
    )


def _market_from_window(window: dict[str, Any]) -> BinaryMarket:
    mid = MarketId(str(window.get("market_id") or "lab-market"))
    start = _parse_ts(window.get("event_start") or "2026-09-13T12:00:00+00:00")
    end = _parse_ts(window.get("event_end") or "2026-09-13T12:05:00+00:00")
    yes = Instrument(
        market_id=mid,
        token_id=TokenId("yes"),
        instrument_id=InstrumentId("yes"),
        outcome=OutcomeSide.YES,
    )
    no = Instrument(
        market_id=mid,
        token_id=TokenId("no"),
        instrument_id=InstrumentId("no"),
        outcome=OutcomeSide.NO,
    )
    return BinaryMarket(
        market_id=mid,
        condition_id=str(window.get("condition_id") or "lab-condition"),
        question=str(window.get("question") or "lab window"),
        yes=yes,
        no=no,
        event_start=start,
        event_end=end,
    )


def _snapshot(
    *,
    market: BinaryMarket,
    books: ReplayBooks,
    ts: datetime,
    reference_price: Decimal | None,
) -> DecisionSnapshot:
    ref = None
    if reference_price is not None:
        ref = ReferencePriceSnapshot(
            symbol="BTCUSD_LAB",
            price=reference_price,
            ts_event=ts,
            venue="lab",
        )
    return DecisionSnapshot(
        market=market,
        yes_book=None,
        no_book=None,
        yes_quote=_quote(ask=books.up_ask, bid=books.up_bid),
        no_quote=_quote(ask=books.down_ask, bid=books.down_bid),
        reference=ref,
        yes_freshness=_fresh(ts),
        no_freshness=_fresh(ts),
        reference_freshness=_fresh(ts),
        observed_at=ts,
        correlation_id=new_correlation_id(),
        causation_id=new_event_id(),
    )


def _quote(*, ask: Decimal | None, bid: Decimal | None) -> ExecutableQuote:
    mid = None
    spread = None
    if ask is not None and bid is not None:
        mid = (ask + bid) / Decimal("2")
        spread = ask - bid
    return ExecutableQuote(
        best_ask=ask,
        best_bid=bid,
        mid=mid,
        spread=spread,
        ask_size_at_touch=Decimal("10") if ask is not None else Decimal("0"),
        bid_size_at_touch=Decimal("10") if bid is not None else Decimal("0"),
    )


def _fresh(ts: datetime) -> FreshnessAssessment:
    return FreshnessAssessment(
        is_fresh=True,
        age_ms=0,
        threshold_ms=5_000,
        timestamp_basis=TimestampBasis.EVENT_TIME,
        reason_code=FreshnessReason.FRESH,
        observed_at=ts,
    )


def _levels(raw: Any) -> tuple[DepthLevel, ...]:
    levels: list[DepthLevel] = []
    for item in raw:
        price, qty = item[0], item[1]
        levels.append(DepthLevel(price=Decimal(str(price)), quantity=Decimal(str(qty))))
    return tuple(levels)


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _parse_ts(value: Any) -> datetime:
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
