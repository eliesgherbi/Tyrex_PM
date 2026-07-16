"""Unit tests for SurvivalExitPlanner (Wave B M2)."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.market_data.executable_book import ExecutableBookView
from tyrex_pm.market_data.models import BookLevel, BookSource, MarketStateSnapshot, SourceQuality
from tyrex_pm.runtime.config import SurvivalExitPlanningConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.market_data_runtime import inject_fixture_book
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.survival.exit_planning import SurvivalExitPlanner
from tyrex_pm.survival.models import ExecutableExitEvidence, SurvivalExitEvaluation

TOKEN = TokenId("9059650700126795019827485089957938050581213031053374092199507389736394347163")


def _snap(
    *,
    bids: tuple[BookLevel, ...],
    book_age_ms: int = 100,
    snapshot_id: str = "snap-1",
) -> MarketStateSnapshot:
    best_bid = bids[0].price if bids else None
    return MarketStateSnapshot(
        token_id=TOKEN,
        bids=bids,
        asks=(BookLevel(Decimal("0.55"), Decimal("100")),),
        best_bid=best_bid,
        best_ask=Decimal("0.55"),
        best_bid_size=bids[0].size if bids else None,
        best_ask_size=Decimal("100"),
        spread=Decimal("0.05"),
        mid=Decimal("0.525"),
        received_ts=utc_now(),
        exchange_ts=None,
        book_age_ms=book_age_ms,
        source=BookSource.FIXTURE,
        source_quality=SourceQuality.REST_BOOTSTRAP,
        sequence=1,
        book_hash=None,
        snapshot_id=snapshot_id,
        reconnect_gap=False,
    )


def _planner(**over) -> SurvivalExitPlanner:
    cfg = SurvivalExitPlanningConfig(
        require_executable_evidence=True,
        allow_partial_survival_exit=True,
        min_depth_fraction=Decimal("0.8"),
        max_book_age_s=None,
    )
    if over:
        cfg = SurvivalExitPlanningConfig(**{**cfg.__dict__, **over})
    return SurvivalExitPlanner(cfg, default_max_book_age_s=5.0)


def _coord_with_snap(snap: MarketStateSnapshot) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    store = MarketStateStore(default_max_age_s=5.0)
    store.apply_book(
        TOKEN,
        list(snap.bids),
        list(snap.asks),
        source=snap.source,
        received_ts=snap.received_ts,
    )
    coord.market_state = store
    return coord


def _coord_fixture(*, stale: bool = False) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    inject_fixture_book(
        coord,
        str(TOKEN),
        best_bid=Decimal("0.85"),
        best_ask=Decimal("0.90"),
        stale=stale,
        size=Decimal("100"),
    )
    return coord


def test_touch_bid_high_insufficient_depth_not_proceed_full() -> None:
    snap = _snap(bids=(BookLevel(Decimal("0.90"), Decimal("1")),))
    coord = _coord_with_snap(snap)
    ev = _planner().evaluate_exit(coord=coord, token_id=TOKEN, qty=Decimal("5"))
    assert ev.verdict != "proceed_full"
    assert ev.evidence.touch_bid == Decimal("0.90")
    assert ev.evidence.available_depth_fraction == Decimal("0.2")


def test_partial_path_when_allow_partial() -> None:
    snap = _snap(bids=(BookLevel(Decimal("0.85"), Decimal("2")),))
    coord = _coord_with_snap(snap)
    ev = _planner(allow_partial_survival_exit=True).evaluate_exit(
        coord=coord, token_id=TOKEN, qty=Decimal("5")
    )
    assert ev.verdict == "proceed_partial"
    assert ev.recommended_qty == Decimal("2")


def test_stale_book_defers() -> None:
    coord = _coord_fixture(stale=True)
    ev = _planner(max_book_age_s=1.0).evaluate_exit(coord=coord, token_id=TOKEN, qty=Decimal("5"))
    assert ev.verdict == "defer"
    assert ev.reason == "stale_book"


def test_executable_bid_for_progress_not_touch_when_thin() -> None:
    bids = (BookLevel(Decimal("0.90"), Decimal("1")), BookLevel(Decimal("0.80"), Decimal("10")))
    view = ExecutableBookView.from_snapshot(
        _snap(bids=bids),
        side=Side.SELL,
        size=Decimal("5"),
    )
    ev = SurvivalExitEvaluation(
        verdict="proceed_partial",
        recommended_qty=Decimal("5"),
        evidence=ExecutableExitEvidence(
            touch_bid=view.touch_price,
            executable_bid=view.worst_price_to_fill,
            sweep_vwap=view.sweep_vwap,
            worst_price_to_fill=view.worst_price_to_fill,
            available_depth=view.available_depth,
            available_depth_fraction=Decimal("0.2"),
            expected_slippage=Decimal("0"),
            book_age_ms=100,
            snapshot_id="x",
            quality_verdict="pass",
            spread=Decimal("0.05"),
            planner_evidence_ref=None,
        ),
        reason="partial_depth",
    )
    progress = _planner().executable_bid_for_progress(ev)
    assert progress == view.sweep_vwap
    assert progress is not None
    assert view.touch_price is not None
    assert progress < view.touch_price


def test_evidence_payload_fields() -> None:
    snap = _snap(bids=(BookLevel(Decimal("0.80"), Decimal("10")),))
    coord = _coord_with_snap(snap)
    ev = _planner().evaluate_exit(coord=coord, token_id=TOKEN, qty=Decimal("5"))
    e = ev.evidence
    assert e.snapshot_id
    assert e.available_depth_fraction == Decimal("1")
    assert e.spread is not None
    assert e.quality_verdict in {"pass", "degraded", "reject_decision", "emergency_only", "unknown"}
