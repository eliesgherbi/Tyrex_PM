"""Tests for Step 2 cleanup: reconcile-fact dedup, wallet_sync emission, and quantization.

These three are observability-only changes to ``runtime.pipeline`` / ``risk.evidence_format``
that previously generated noise in ``facts.jsonl``:

* repeated unchanged reconciles flooded the file
* there was no positive evidence the wallet/positions REST safety net was firing
* deployment USD figures rendered with 24+ decimal-tail noise (e.g. ``"4.000000000000000000000000002"``)
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId, VenueOrderId
from tyrex_pm.core.models import OpenOrderView, WalletPosition
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_RECONCILE,
    FACT_TYPE_WALLET_SYNC,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.risk.evidence_format import q_usd, s_usd, s_usd_map
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.pipeline import (
    _reconcile_signature,
    _wallet_sync_signature,
    emit_wallet_sync,
    reconcile_coordinator,
)
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore


def _read_facts(path: Path) -> list[dict]:
    import json

    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# -----------------------------------------------------------------------------
# Step 2D: quantization
# -----------------------------------------------------------------------------


def test_q_usd_truncates_decimal_tail_noise():
    """The headline pathology: arithmetic noise like 4.000000000000000000000000002."""
    noisy = Decimal("4") + Decimal("0.000000000000000000000000002")
    assert s_usd(noisy) == "4.000000"
    # Bankers' rounding is stable: 0.0000005 rounds to even (0).
    assert s_usd(Decimal("0.0000005")) == "0.000000"
    assert s_usd(Decimal("0.0000015")) == "0.000002"


def test_s_usd_passes_through_none():
    assert s_usd(None) is None


def test_s_usd_map_quantizes_per_token_breakdown():
    out = s_usd_map({"tok-A": Decimal("4.000000000000000000000000002"), "tok-B": Decimal("1.5")})
    assert out == {"tok-A": "4.000000", "tok-B": "1.500000"}


def test_q_usd_returns_decimal_for_arithmetic():
    """Callers may want to keep working with the quantized number (not just str)."""
    assert isinstance(q_usd(Decimal("4")), Decimal)


def test_q_usd_handles_clob_default_allowance_10_to_30():
    """``clob_wallet_sync`` defaults missing allowance to ``10**30`` — must not raise.

    The default Decimal precision (28 digits) blows up trying to quantize ``10**30`` to
    6 decimals (would need 36 sig figs). evidence_format uses a local 40-digit context
    so this is silently handled. Regression for the live crash observed in
    ``live_test_min_size_and_cleanup`` first run.
    """
    huge = Decimal(10) ** 30
    out = s_usd(huge)
    assert out is not None
    # Must still be a recognizable representation of the input magnitude.
    assert out.startswith("1000000000000000000000000000000")


def test_q_usd_handles_extreme_inputs_without_raising():
    """Pathological inputs beyond even the 40-digit context fall back to whole-USD rounding."""
    # 10**45 exceeds a 40-digit precision context for 6-decimal quantization.
    extreme = Decimal(10) ** 45
    out = s_usd(extreme)
    assert out is not None  # never raises


# -----------------------------------------------------------------------------
# Step 2B: reconcile dedup
# -----------------------------------------------------------------------------


def test_reconcile_dedup_collapses_unchanged_signatures(tmp_path: Path) -> None:
    """Three back-to-back reconciles with no state change must produce a single fact."""
    facts = tmp_path / "facts.jsonl"
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore())
    with JsonlSink(facts) as sink:
        for _ in range(3):
            reconcile_coordinator(coord, sink, "run-dedup")
    rows = _read_facts(facts)
    reconciles = [r for r in rows if r["fact_type"] == FACT_TYPE_RECONCILE]
    assert len(reconciles) == 1, "dedup must collapse identical reconciles"


def test_reconcile_dedup_emits_again_when_state_changes(tmp_path: Path) -> None:
    """A change in the operator-relevant signature must produce a new fact."""
    facts = tmp_path / "facts.jsonl"
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore())
    with JsonlSink(facts) as sink:
        reconcile_coordinator(coord, sink, "run-dedup")
        # Inject a foreign-looking REST open order — reconcile will flag drift.
        coord.wallet._rest_open_orders = (
            OpenOrderView(
                token_id=TokenId("tok-X"),
                side=Side.BUY,
                remaining_size=Decimal("10"),
                limit_price=Decimal("0.5"),
                client_order_id=None,
                venue_order_id=VenueOrderId("0xfresh"),
                venue_state_source="rest",
            ),
        )
        coord.wallet.rebuild_open_orders_merged()
        reconcile_coordinator(coord, sink, "run-dedup")
    rows = _read_facts(facts)
    reconciles = [r for r in rows if r["fact_type"] == FACT_TYPE_RECONCILE]
    assert len(reconciles) == 2, "state change must produce a new fact"
    assert reconciles[1]["payload"]["drift_flags"] != reconciles[0]["payload"]["drift_flags"]


def test_reconcile_signature_includes_decision_counts() -> None:
    """A new repair / adoption decision row must trip the signature even if flags match."""

    class FakeRes:
        drift_flags = ()
        blocking_drift_flags = ()
        reconcile_severity = "ok"
        provisional_repair_decisions = ()
        venue_adoption_decisions = ()
        provisional_timeout_resolutions = ()
        pruned_terminal_venue_order_ids = ()

    a = _reconcile_signature(FakeRes(), ())

    class FakeRes2(FakeRes):
        provisional_repair_decisions = ({"client_order_id": "cid-1"},)

    b = _reconcile_signature(FakeRes2(), ())
    assert a != b


# -----------------------------------------------------------------------------
# Step 2C: wallet_sync fact
# -----------------------------------------------------------------------------


def _wallet_with_state() -> WalletStore:
    w = WalletStore()
    w.usdc_balance = Decimal("100.5")
    w.usdc_allowance = Decimal("1000")
    w.last_sync_ts = datetime.now(timezone.utc)
    w.last_positions_sync_ts = datetime.now(timezone.utc)
    w.positions = {
        TokenId("tok-A"): WalletPosition(
            token_id=TokenId("tok-A"), qty=Decimal("10"), avg_price_usd=Decimal("0.4")
        ),
        TokenId("tok-B"): WalletPosition(
            token_id=TokenId("tok-B"), qty=Decimal("5"), avg_price_usd=None
        ),
    }
    return w


def test_wallet_sync_emits_expected_payload(tmp_path: Path) -> None:
    facts = tmp_path / "facts.jsonl"
    coord = RuntimeCoordinator(wallet=_wallet_with_state(), orders=OrderStore())

    with JsonlSink(facts) as sink:
        emit_wallet_sync(coord, sink, "run-ws")

    rows = _read_facts(facts)
    sync_rows = [r for r in rows if r["fact_type"] == FACT_TYPE_WALLET_SYNC]
    assert len(sync_rows) == 1
    p = sync_rows[0]["payload"]
    assert p["wallet_usdc_balance"] == "100.500000"
    assert p["wallet_usdc_allowance"] == "1000.000000"
    assert p["last_sync_ts"] is not None
    assert p["last_positions_sync_ts"] is not None
    assert p["position_count"] == 2
    assert p["open_order_count"] == 0
    assert p["marks_present_count"] == 1
    assert p["marks_missing_count"] == 1


def test_wallet_sync_dedups_when_nothing_changed(tmp_path: Path) -> None:
    facts = tmp_path / "facts.jsonl"
    coord = RuntimeCoordinator(wallet=_wallet_with_state(), orders=OrderStore())
    with JsonlSink(facts) as sink:
        for _ in range(3):
            emit_wallet_sync(coord, sink, "run-ws-dedup")
    rows = _read_facts(facts)
    assert sum(1 for r in rows if r["fact_type"] == FACT_TYPE_WALLET_SYNC) == 1


def test_wallet_sync_reemits_when_balance_moves(tmp_path: Path) -> None:
    facts = tmp_path / "facts.jsonl"
    coord = RuntimeCoordinator(wallet=_wallet_with_state(), orders=OrderStore())
    with JsonlSink(facts) as sink:
        emit_wallet_sync(coord, sink, "run-ws-move")
        coord.wallet.usdc_balance = Decimal("90.5")  # balance moved
        emit_wallet_sync(coord, sink, "run-ws-move")
    rows = _read_facts(facts)
    sync_rows = [r for r in rows if r["fact_type"] == FACT_TYPE_WALLET_SYNC]
    assert len(sync_rows) == 2
    assert sync_rows[0]["payload"]["wallet_usdc_balance"] == "100.500000"
    assert sync_rows[1]["payload"]["wallet_usdc_balance"] == "90.500000"


def test_wallet_sync_signature_keys_on_relevant_fields() -> None:
    coord = RuntimeCoordinator(wallet=_wallet_with_state(), orders=OrderStore())
    s1 = _wallet_sync_signature(coord)
    # Position count alone should change the signature.
    coord.wallet.positions[TokenId("tok-C")] = WalletPosition(
        token_id=TokenId("tok-C"), qty=Decimal("1"), avg_price_usd=Decimal("0.5")
    )
    s2 = _wallet_sync_signature(coord)
    assert s1 != s2


def test_wallet_sync_signature_ignores_refresh_timestamps() -> None:
    """Refresh timestamps must NOT participate in the dedup signature.

    Regression guard for the bug observed in ``var/reporting/runs/live_tes_700``: a 30 s
    REST refresh loop emitted a fresh ``wallet_sync`` fact every tick because the signature
    included ``last_sync_ts`` / ``last_positions_sync_ts``, which advance unconditionally on
    each successful refresh. Net effect was 70/97 facts being operator-meaningless duplicates.
    """
    coord = RuntimeCoordinator(wallet=_wallet_with_state(), orders=OrderStore())
    s1 = _wallet_sync_signature(coord)
    # Simulate a successful REST tick that did not change anything actionable.
    coord.wallet.last_sync_ts = datetime(2026, 4, 18, 12, 48, 0, tzinfo=timezone.utc)
    coord.wallet.last_positions_sync_ts = datetime(
        2026, 4, 18, 12, 48, 1, tzinfo=timezone.utc
    )
    s2 = _wallet_sync_signature(coord)
    assert s1 == s2, "refresh timestamps must not change the dedup signature"


def test_wallet_sync_dedups_across_refresh_ticks(tmp_path: Path) -> None:
    """End-to-end: emit_wallet_sync must suppress no-op refresh ticks even when timestamps move.

    Pairs with ``test_wallet_sync_signature_ignores_refresh_timestamps`` to lock in the live
    behaviour: 5 successive REST ticks with bumped ``last_sync_ts`` but unchanged operator
    state produce exactly one wallet_sync fact, not five.
    """
    facts = tmp_path / "facts.jsonl"
    coord = RuntimeCoordinator(wallet=_wallet_with_state(), orders=OrderStore())
    with JsonlSink(facts) as sink:
        for i in range(5):
            coord.wallet.last_sync_ts = datetime(
                2026, 4, 18, 12, 48, i, tzinfo=timezone.utc
            )
            coord.wallet.last_positions_sync_ts = datetime(
                2026, 4, 18, 12, 48, i, 500_000, tzinfo=timezone.utc
            )
            emit_wallet_sync(coord, sink, "run-ws-noop-ticks")
    rows = _read_facts(facts)
    assert sum(1 for r in rows if r["fact_type"] == FACT_TYPE_WALLET_SYNC) == 1
