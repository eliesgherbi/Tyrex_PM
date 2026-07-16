"""Endpoint category helpers for R6C evidence (no secrets)."""

from __future__ import annotations

from tyrex_pm.execution.polymarket.readonly_transport import (
    AUTH_CLOB_BALANCE,
    AUTH_CLOB_HEARTBEAT,
    AUTH_CLOB_ORDERS,
    AUTH_CLOB_TRADES,
    PUBLIC_CLOB_BOOK,
    PUBLIC_CLOB_TIME,
    PUBLIC_DATA_POSITIONS,
)

PUBLIC_MARKET_DATA = (
    PUBLIC_CLOB_TIME,
    PUBLIC_CLOB_BOOK,
    ("clob.polymarket.com", "/price", "GET", "public_market_data"),
    ("clob.polymarket.com", "/spread", "GET", "public_market_data"),
    ("gamma-api.polymarket.com", "/markets", "GET", "public_market_data"),
)

AUTHENTICATED_ACCOUNT = (
    AUTH_CLOB_ORDERS,
    AUTH_CLOB_TRADES,
    AUTH_CLOB_BALANCE,
)

PUBLIC_DATA_API = (PUBLIC_DATA_POSITIONS,)

MUTATING_DO_NOT_CALL_R6C = (AUTH_CLOB_HEARTBEAT,)


def classify_endpoint(host: str, path: str, method: str) -> str:
    key = (host, path.split("?", 1)[0], method.upper())
    for row in PUBLIC_MARKET_DATA:
        if (row[0], row[1], row[2]) == key:
            return row[3]
    for row in AUTHENTICATED_ACCOUNT:
        if (row[0], row[1], row[2]) == key:
            return row[3]
    for row in PUBLIC_DATA_API:
        if (row[0], row[1], row[2]) == key:
            return row[3]
    for row in MUTATING_DO_NOT_CALL_R6C:
        if (row[0], row[1], row[2]) == key:
            return row[3]
    if method.upper() in {"POST", "DELETE", "PUT", "PATCH"}:
        return "authenticated_mutating_or_unknown"
    return "unknown"
