"""Canonical reporting identifiers (SCH-01).

See ``joins.md`` for the join matrix.
"""

from __future__ import annotations

# Fact envelope MUST include:
# - run_id (uuid string)
# - fact_schema_version (int)
# - fact_type (str)
# - recorded_at_utc (ISO-8601 Z or +00:00)

# Guru-follow join spine:
# correlation_id == GuruTradeSignal.source_trade_id == OrderIntent.correlation_id

# Framework path:
# client_order_id == str(Nautilus ClientOrderId) from nautilus_guru_exec (deterministic SHA256-based).
# order_correlation_map_fact links correlation_id <-> client_order_id at submit time.

# venue_order_id may be null on early lifecycle rows; use client_order_id as spine.
