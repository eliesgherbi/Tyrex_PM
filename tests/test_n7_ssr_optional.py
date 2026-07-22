"""N7 PTB readiness: optional SSR openPrice match (disabled by default for live testing)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from tyrex_pm.adapters.polymarket.ssr_ptb_attestation import (
    DisabledSsrAttestationProvider,
    SsrDisplayedPtbAttestationProvider,
)
from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import MarketId, TokenId
from tyrex_pm.core.ingress import FeedRole, IngressMeta
from tyrex_pm.domain.polymarket.boundary_candidates import BoundaryTickView
from tyrex_pm.domain.polymarket.market import BinaryMarket, MarketStatus, make_binary_instruments
from tyrex_pm.domain.polymarket.ptb_attestation import (
    AttestationResult,
    FixturePtbAttestationProvider,
)
from tyrex_pm.indicators.causal_pairing import PriceTickView, TradingReferenceIdentity
from tyrex_pm.runtime.n4_observe_runtime import N4ObserveRuntime, SessionSlot
from tyrex_pm.runtime.n7_ptb_policy import no_entry_reason, ptb_trust_fields
from tyrex_pm.runtime.n7_sealed import load_n7_sealed_config, n7_sealed_from_mapping
from tyrex_pm.strategies.z_gap.config import ZGapConfig, ZGapPtbTimeQualityConfig

REPO = Path(__file__).resolve().parents[1]


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


START = "2026-07-20T21:15:00+00:00"
END = "2026-07-20T21:20:00+00:00"


def _market(window_id: str = "w-ssr") -> BinaryMarket:
    mid = MarketId("m-ssr")
    yes, no = make_binary_instruments(
        market_id=mid,
        yes_token=TokenId(f"up-{window_id}"),
        no_token=TokenId(f"down-{window_id}"),
    )
    return BinaryMarket(
        market_id=mid,
        condition_id=window_id,
        question=window_id,
        yes=yes,
        no=no,
        event_start=_ts(START),
        event_end=_ts(END),
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
        status=MarketStatus.ACTIVE,
    )


def _cl(value: str, fp: str = "cl") -> BoundaryTickView:
    return BoundaryTickView(
        value=Decimal(value),
        source_ts=_ts(START),
        receive_wall_raw_utc=_ts(START) + timedelta(seconds=1),
        receive_wall_corrected_utc=_ts(START) + timedelta(seconds=1),
        receive_monotonic_ns=2,
        raw_fingerprint=fp,
        event_id=fp,
        ingress=IngressMeta(
            receive_monotonic_ns=2,
            ingress_sequence=2,
            connection_generation=1,
            receive_wall_raw_utc=_ts(START) + timedelta(seconds=1),
            receive_wall_corrected_utc=_ts(START) + timedelta(seconds=1),
            clock_status="READY",
            clock_offset_ms=0.0,
            clock_uncertainty_ms=50,
            raw_fingerprint=fp,
            role=FeedRole.SETTLEMENT_REFERENCE,
        ),
    )


def _bn(value: str = "100.1") -> PriceTickView:
    return PriceTickView(
        value=Decimal(value),
        source_ts=_ts(START) - timedelta(seconds=1),
        receive_wall_raw_utc=_ts(START),
        receive_monotonic_ns=1,
        identity=TradingReferenceIdentity.BINANCE_SPOT,
        raw_fingerprint="bn",
    )


def _runtime(
    *,
    require_ssr: bool,
    attested: str | None,
    attestation_source: str = "fixture",
) -> N4ObserveRuntime:
    clock = FakeClock(_wall=_ts(START))
    if attested is None:
        port = FixturePtbAttestationProvider(by_window={})
    else:
        port = FixturePtbAttestationProvider(
            by_window={
                "w-ssr": (attested, attestation_source, {"fixture": True}),
            }
        )
    return N4ObserveRuntime.create(
        clock=clock,
        attestation_port=port,
        basis_ewma_half_life_s=30.0,
        zgap_config=ZGapConfig(
            ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=Decimal("10000"))
        ),
        require_ssr_price_match=require_ssr,
    )


def _seal_ready(rt: N4ObserveRuntime, *, skip_attestation: bool = False):
    m = _market()
    sess = rt.open_session(slot=SessionSlot.ACTIVE, market=m, window_id="w-ssr")
    sess.up_ask, sess.up_bid = Decimal("0.55"), Decimal("0.52")
    sess.down_ask, sess.down_bid = Decimal("0.48"), Decimal("0.45")
    rt.ingest_binance(_bn())
    rt.ingest_chainlink(window_id="w-ssr", market_id=m.market_id, tick=_cl("100"))
    sealed = rt.attest_and_seal(
        market_id=m.market_id,
        window_id="w-ssr",
        sealed_at=_ts(START) + timedelta(seconds=5),
        skip_attestation=skip_attestation,
    )
    return m, sealed


def test_active_n7_config_disables_ssr_match() -> None:
    cfg = load_n7_sealed_config(REPO / "config" / "n7_tiny_live.json")
    assert cfg.require_ssr_price_match is False
    assert isinstance(cfg.require_ssr_price_match, bool)


def test_require_ssr_must_be_boolean() -> None:
    with pytest.raises(ValueError, match="boolean"):
        n7_sealed_from_mapping(
            {
                "require_ssr_price_match": "false",
                "live": {"enabled": False, "mutations_enabled": False, "scope": "A"},
            }
        )


def test_valid_sealed_k_evaluates_when_ssr_absent() -> None:
    rt = _runtime(require_ssr=False, attested=None)
    _, sealed = _seal_ready(rt, skip_attestation=True)
    assert sealed.ptb_k == Decimal("100")
    assert sealed.ptb_attestation_result is AttestationResult.INCOMPLETE
    ready = rt.prepare_aligned_eval()
    assert ready.ok is True
    assert "attestation_unavailable" not in ready.skip_reasons
    assert ready.model_anchor == sealed.ptb_k


def test_null_attested_value_does_not_block_when_disabled() -> None:
    rt = _runtime(require_ssr=False, attested=None)
    _seal_ready(rt)  # attest → incomplete
    ready = rt.prepare_aligned_eval()
    assert ready.ok is True
    assert ready.sealed is not None
    assert ready.sealed.ptb_attestation_result is AttestationResult.INCOMPLETE


def test_ssr_incomplete_does_not_block_when_disabled() -> None:
    rt = _runtime(require_ssr=False, attested=None)
    _seal_ready(rt)
    ready = rt.prepare_aligned_eval()
    assert ready.ok is True


def test_ssr_mismatch_does_not_block_when_disabled() -> None:
    rt = _runtime(require_ssr=False, attested="99999")
    _, sealed = _seal_ready(rt)
    assert sealed.ptb_attestation_result is AttestationResult.MISMATCH
    ready = rt.prepare_aligned_eval()
    assert ready.ok is True
    assert "attestation_mismatch" not in ready.skip_reasons
    assert ready.model_anchor == Decimal("100")


def test_ssr_fetch_failure_does_not_block_when_disabled() -> None:
    class _Boom:
        def fetch_attestation(self, **kwargs):  # noqa: ANN003
            raise ConnectionError("ssr down")

    clock = FakeClock(_wall=_ts(START))
    rt = N4ObserveRuntime.create(
        clock=clock,
        attestation_port=_Boom(),  # type: ignore[arg-type]
        basis_ewma_half_life_s=30.0,
        zgap_config=ZGapConfig(
            ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=Decimal("10000"))
        ),
        require_ssr_price_match=False,
    )
    # Skip attestation entirely (disabled path never fetches).
    _, sealed = _seal_ready(rt, skip_attestation=True)
    assert sealed.ptb_k == Decimal("100")
    assert rt.prepare_aligned_eval().ok is True


def test_no_ssr_network_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _forbid(slug: str, *, timeout_s: float = 30.0) -> str:  # noqa: ARG001
        calls.append(slug)
        raise AssertionError("SSR HTTP must not be called when disabled")

    monkeypatch.setattr(
        "tyrex_pm.adapters.polymarket.ssr_ptb_attestation.fetch_event_html",
        _forbid,
    )
    DisabledSsrAttestationProvider.fetch_count = 0
    port = DisabledSsrAttestationProvider()
    with pytest.raises(RuntimeError, match="disabled"):
        port.fetch_attestation(
            market_id=MarketId("m"),
            window_id="btc-updown-5m-1",
            event_start=_ts(START),
        )
    assert DisabledSsrAttestationProvider.fetch_count == 1
    assert calls == []

    # Live provider must not be invoked either when compose installs None port.
    live = SsrDisplayedPtbAttestationProvider(retries=1)
    with patch.object(live, "fetch_attestation", side_effect=AssertionError("no fetch")):
        rt = N4ObserveRuntime.create(
            clock=FakeClock(_wall=_ts(START)),
            attestation_port=None,
            basis_ewma_half_life_s=30.0,
            zgap_config=ZGapConfig(
                ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=Decimal("10000"))
            ),
            require_ssr_price_match=False,
        )
        _seal_ready(rt, skip_attestation=True)
        assert rt.prepare_aligned_eval().ok is True


def test_zgap_receives_immutable_chainlink_sealed_k() -> None:
    rt = _runtime(require_ssr=False, attested="99999")
    _, sealed = _seal_ready(rt)
    ready = rt.prepare_aligned_eval()
    assert ready.ok is True
    assert ready.model_anchor == sealed.ptb_k == Decimal("100")
    assert ready.model_anchor != Decimal("99999")


def test_missing_chainlink_seal_still_blocks() -> None:
    rt = _runtime(require_ssr=False, attested=None)
    m = _market()
    rt.open_session(slot=SessionSlot.ACTIVE, market=m, window_id="w-ssr")
    rt.ingest_binance(_bn())
    # No EXACT chainlink → no seal
    ready = rt.prepare_aligned_eval()
    assert ready.ok is False
    assert "exact_candidate_absent" in ready.skip_reasons


def test_require_ssr_true_restores_strict_match_gate() -> None:
    rt = _runtime(require_ssr=True, attested=None)
    _seal_ready(rt)
    ready = rt.prepare_aligned_eval()
    assert ready.ok is False
    assert "attestation_unavailable" in ready.skip_reasons

    rt2 = _runtime(require_ssr=True, attested="99999")
    _seal_ready(rt2)
    ready2 = rt2.prepare_aligned_eval()
    assert ready2.ok is False
    assert "attestation_mismatch" in ready2.skip_reasons

    rt3 = _runtime(require_ssr=True, attested="100")
    _seal_ready(rt3)
    ready3 = rt3.prepare_aligned_eval()
    assert ready3.ok is True


def test_report_fields_state_ssr_disabled_or_required() -> None:
    off = ptb_trust_fields(
        sealed_k="100",
        require_ssr_price_match=False,
        ptb_ready=True,
    )
    assert off["ptb_authority"] == "chainlink_sealed_k"
    assert off["ssr_match_required"] is False
    assert off["ssr_check_status"] == "DISABLED"
    assert off["ptb_ready"] is True
    assert off["sealed_k"] == "100"

    on = ptb_trust_fields(
        sealed_k="100",
        require_ssr_price_match=True,
        ptb_ready=False,
        ssr_check_status="REQUIRED",
    )
    assert on["ptb_authority"] == "chainlink_sealed_k_and_ssr_match"
    assert on["ssr_match_required"] is True
    assert on["ssr_check_status"] == "REQUIRED"

    reason = no_entry_reason(
        evals=2,
        last_skip_reasons=["attestation_unavailable"],
        require_ssr_price_match=False,
    )
    assert reason == "evaluated_no_enter_signal"
    assert "attestation_unavailable" not in reason
    assert "intentional_no_signal_or_wait" not in reason


def test_fake_oneshot_lifecycle_without_ssr(tmp_path: Path) -> None:
    from tyrex_pm.runtime.n7_operator_run import run_fake_oneshot_rehearsal

    result = run_fake_oneshot_rehearsal(
        out_dir=tmp_path / "fake",
        config_path=REPO / "config" / "n7_tiny_live.json",
    )
    assert result.ok is True
    assert result.outcome == "PASS_FAKE_FLAT"
    assert result.payload["real_venue_mutations"] == 0
    assert result.payload["ssr_match_required"] is False
    assert result.payload["ssr_check_status"] == "DISABLED"
    assert result.payload["ptb_authority"] == "chainlink_sealed_k"
    assert result.payload["ptb_ready"] is True
    assert int(result.payload["evals"]) >= 1
