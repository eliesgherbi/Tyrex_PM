"""Fill latency + histogram helpers over synthetic facts."""

from __future__ import annotations

from tyrex_pm.reporting.summarize import _fill_latency_ms_stats


def test_fill_latency_median() -> None:
    facts = [
        {
            "fact_type": "order_lifecycle",
            "client_order_id": "TXa",
            "ts_event_ns": 1_000_000_000,
        },
        {
            "fact_type": "order_lifecycle",
            "client_order_id": "TXa",
            "ts_event_ns": 1_100_000_000,
        },
        {
            "fact_type": "fill",
            "client_order_id": "TXa",
            "ts_event_ns": 1_500_000_000,
        },
    ]
    med, n = _fill_latency_ms_stats(facts)
    assert n == 1
    assert med == 500.0
