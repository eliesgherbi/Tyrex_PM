"""VAL-02: join smoke after REC-05 (signal → intent orphans)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tyrex_pm.reporting.etl.jsonl_to_sqlite import build_sqlite_from_jsonl
from tyrex_pm.reporting.schema.facts_v1 import fact_envelope

_ISO = "2026-04-05T12:00:00+00:00"


def test_sqlite_join_orphan_reportable() -> None:
    rid = "join-test"
    rows = [
        fact_envelope(
            fact_type="guru_signal",
            run_id=rid,
            recorded_at_utc=_ISO,
            payload={
                "correlation_id": "c_ok",
                "source": "poll",
                "side": "BUY",
                "token_id": "t1",
                "ts_event_ms": 1,
                "ts_emit_ms": 2,
                "guru_size_raw": 1.0,
                "guru_price_raw": 0.5,
            },
        ),
        fact_envelope(
            fact_type="execution_intent",
            run_id=rid,
            recorded_at_utc=_ISO,
            payload={
                "correlation_id": "c_ok",
                "token_id": "t1",
                "side": "BUY",
                "quantity": 1.0,
                "signal_kind": "entry",
            },
        ),
        fact_envelope(
            fact_type="guru_signal",
            run_id=rid,
            recorded_at_utc=_ISO,
            payload={
                "correlation_id": "c_orphan",
                "source": "poll",
                "side": "BUY",
                "token_id": "t2",
                "ts_event_ms": 1,
                "ts_emit_ms": 2,
                "guru_size_raw": 1.0,
                "guru_price_raw": 0.5,
            },
        ),
    ]
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        fp = d / "facts.jsonl"
        with fp.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        build_sqlite_from_jsonl(d)
        import sqlite3

        con = sqlite3.connect(str(d / "run.sqlite"))
        try:
            cur = con.execute(
                """
                SELECT correlation_id FROM facts
                WHERE fact_type = 'guru_signal'
                  AND correlation_id NOT IN (
                    SELECT correlation_id FROM facts
                    WHERE fact_type = 'execution_intent' AND correlation_id IS NOT NULL
                  )
                """,
            )
            orphans = {row[0] for row in cur.fetchall()}
        finally:
            con.close()
    assert orphans == {"c_orphan"}
