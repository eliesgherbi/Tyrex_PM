"""VAL-01: every fact kind validates via :func:`fact_envelope`."""

from __future__ import annotations

import pytest

from tyrex_pm.reporting.schema.facts_v1 import FactValidationError, _REQUIRED, fact_envelope

_ISO = "2026-04-05T12:00:00+00:00"
_RID = "unit-test-run"


def _p(ft: str) -> dict:
    """Minimal valid payload per ``fact_type`` for v1."""
    if ft == "run_manifest":
        return {
            "run_id": _RID,
            "started_at_utc": _ISO,
            "trader_id": "T1",
            "execution_mode": "shadow",
            "guru_ingest_mode": "poll_only",
            "execution_path": "shadow",
        }
    if ft == "config_snapshot":
        return {"run_id": _RID, "config_sha256": "abc", "config_json": "{}"}
    if ft == "guru_signal":
        return {
            "correlation_id": "c1",
            "source": "poll",
            "side": "BUY",
            "token_id": "t1",
            "ts_event_ms": 1,
            "ts_emit_ms": 2,
            "guru_size_raw": 1.0,
            "guru_price_raw": 0.5,
        }
    if ft == "guru_shadow_compare":
        return {
            "correlation_id": "c1",
            "side": "BUY",
            "token_id": "t1",
            "ts_event_ms": 1,
            "ts_recv_ms": 2,
            "would_publish_new": True,
        }
    if ft == "health_anomaly":
        return {"component": "guru_monitor", "event_type": "poll_error"}
    if ft == "strategy_decision":
        return {"correlation_id": "c1", "branch": "entry", "decision": "skip", "reason_code": "x"}
    if ft == "sizing":
        return {"correlation_id": "c1", "target_qty": 1.0, "signal_branch": "entry"}
    if ft == "risk_decision":
        return {"correlation_id": "c1", "allowed": False, "reason_code": "risk_kill_switch"}
    if ft == "execution_intent":
        return {
            "correlation_id": "c1",
            "token_id": "t1",
            "side": "BUY",
            "quantity": 1.0,
            "signal_kind": "entry",
        }
    if ft == "normalization":
        return {
            "correlation_id": "c1",
            "skipped_submit": True,
            "reason_code": "exec_venue_normalize_skip",
            "pre_qty": 1.0,
            "post_qty": 0.0,
            "pre_price": 0.5,
            "post_price": 0.5,
        }
    if ft == "book_constraint":
        return {"correlation_id": "c1", "book_source": "cache"}
    if ft == "execution_outcome":
        return {
            "correlation_id": "c1",
            "outcome": "submit",
            "reason_code": "live_order_submit",
            "instrument_id": "i1",
            "submitted_qty": 1.0,
            "submitted_price": 0.5,
        }
    if ft == "order_correlation_map":
        return {"correlation_id": "c1", "client_order_id": "TX" + "a" * 26, "instrument_id": "i1"}
    if ft == "order_lifecycle":
        return {"client_order_id": "TX" + "a" * 26, "status": "SUBMITTED"}
    if ft == "fill":
        return {"client_order_id": "TX" + "a" * 26, "fill_event_id": "f1"}
    if ft == "account_snapshot":
        return {
            "account_snapshot_seq": 1,
            "account_present": True,
            "snapshot_trigger": "risk_eval",
            "captured_at_utc": _ISO,
        }
    if ft == "deployment_budget":
        return {
            "correlation_id": "c1",
            "order_deploy_usd": 1.0,
            "token_pending_usd": 0.0,
            "token_filled_usd": 0.0,
            "token_deploy_usd": 0.0,
            "portfolio_pending_usd": 0.0,
            "portfolio_filled_usd": 0.0,
            "portfolio_deploy_usd": 0.0,
        }
    if ft == "position":
        return {"instrument_id": "i1"}
    if ft == "component_status":
        return {"component": "warmup", "status": "ok"}
    if ft == "report_pipeline_health":
        return {"flush_ok": True}
    if ft == "reconciliation":
        return {"check_type": "submit_vs_cache", "outcome": "match"}
    raise AssertionError(f"add golden payload for {ft}")


@pytest.mark.parametrize("fact_type", sorted(_REQUIRED.keys()))
def test_golden_fact_envelope_validates(fact_type: str) -> None:
    payload = _p(fact_type)
    fact_envelope(
        fact_type=fact_type,
        run_id=_RID,
        recorded_at_utc=_ISO,
        payload=payload,
    )


def test_unknown_fact_rejected() -> None:
    with pytest.raises(FactValidationError):
        fact_envelope(
            fact_type="not_a_real_fact",
            run_id=_RID,
            recorded_at_utc=_ISO,
            payload={},
        )
