"""Load ``facts.jsonl`` into ``run.sqlite`` (REC-05)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def _col(row: dict[str, Any], key: str) -> str | None:
    v = row.get(key)
    if v is None:
        return None
    return str(v)


def _extract_join_columns(
    row: dict[str, Any],
) -> tuple[str | None, str | None, str | None, str | None]:
    return (
        _col(row, "correlation_id"),
        _col(row, "client_order_id"),
        _col(row, "token_id"),
        _col(row, "instrument_id"),
    )



def build_sqlite_from_jsonl(
    run_dir: Path,
    *,
    facts_name: str = "facts.jsonl",
    db_name: str = "run.sqlite",
) -> Path:
    """
    Create ``run_dir / db_name`` with a ``facts`` table: one row per JSONL object,
    plus extracted join columns for index-friendly queries.
    """
    run_dir = run_dir.resolve()
    facts_path = run_dir / facts_name
    db_path = run_dir / db_name
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS facts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              fact_schema_version INTEGER NOT NULL,
              fact_type TEXT NOT NULL,
              run_id TEXT NOT NULL,
              recorded_at_utc TEXT NOT NULL,
              correlation_id TEXT,
              client_order_id TEXT,
              token_id TEXT,
              instrument_id TEXT,
              payload_json TEXT NOT NULL
            )
            """,
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_type ON facts(fact_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_corr ON facts(correlation_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_coid ON facts(client_order_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_token ON facts(token_id)")
        conn.execute("DELETE FROM facts")
        if facts_path.is_file():
            with facts_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    fs = int(row.get("fact_schema_version") or 0)
                    ft = str(row.get("fact_type") or "")
                    rid = str(row.get("run_id") or "")
                    ts = str(row.get("recorded_at_utc") or "")
                    c1, c2, c3, c4 = _extract_join_columns(row)
                    conn.execute(
                        """
                        INSERT INTO facts (
                          fact_schema_version, fact_type, run_id, recorded_at_utc,
                          correlation_id, client_order_id, token_id, instrument_id,
                          payload_json
                        ) VALUES (?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            fs,
                            ft,
                            rid,
                            ts,
                            c1,
                            c2,
                            c3,
                            c4,
                            json.dumps(row, ensure_ascii=False, default=str),
                        ),
                    )
        conn.commit()
    finally:
        conn.close()
    return db_path
