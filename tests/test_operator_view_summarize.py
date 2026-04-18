from __future__ import annotations

from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_GURU_POLL, FACT_TYPE_HEALTH
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.reporting.summarize import operator_hollow_run_view, summarize_run


def test_operator_view_flags_zero_new_signals(tmp_path) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    with JsonlSink(run_dir / "facts.jsonl") as sink:
        sink.write(make_fact(FACT_TYPE_HEALTH, "r1", {"status": "started"}))
        sink.write(
            make_fact(
                FACT_TYPE_GURU_POLL,
                "r1",
                {
                    "source": "data_api",
                    "new_signals": 0,
                    "raw_rows": 3,
                    "guru_wallet_configured": True,
                },
            )
        )
        sink.write(make_fact(FACT_TYPE_HEALTH, "r1", {"status": "stopped"}))
    s = summarize_run(run_dir)
    notes = s["operator_view"]["operator_notes"]
    assert any("new_signals=0" in n for n in notes)


def test_operator_hollow_no_poll_old_run() -> None:
    rows = [
        {"fact_type": FACT_TYPE_HEALTH, "payload": {"status": "started"}},
        {"fact_type": FACT_TYPE_HEALTH, "payload": {"status": "stopped"}},
    ]
    v = operator_hollow_run_view(rows)
    assert v["operator_notes"]
