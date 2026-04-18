from __future__ import annotations

import logging
import os
import re
from uuid import uuid4

from typing import Any

log = logging.getLogger(__name__)

_UUID_HYPHEN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def normalize_heartbeat_id_for_clob(heartbeat_id: str | None) -> str:
    """
    Value for JSON heartbeat_id on POST /v1/heartbeats.

    Polymarket: first request uses **empty string**; later requests use server-provided id
    (hyphenated UUID in error/success bodies is normalized to 32-char hex).
    """
    if heartbeat_id is None:
        return ""
    s = str(heartbeat_id).strip()
    if not s:
        return ""
    if _UUID_HYPHEN.match(s):
        return s.replace("-", "").lower()
    return s


def resolve_clob_heartbeat_id() -> str:
    """
    Heartbeat id for POST /v1/heartbeats.

    Env TYREX_HEARTBEAT_ID or POLYMARKET_HEARTBEAT_ID (optional). Hyphenated UUIDs become
    32-char hex. If unset, a random hex id is used.

    Only the supervisor loop should POST heartbeats (no duplicate bootstrap POST).
    """
    raw = (
        (os.environ.get("TYREX_HEARTBEAT_ID") or "").strip()
        or (os.environ.get("POLYMARKET_HEARTBEAT_ID") or "").strip()
    )
    if not raw:
        return uuid4().hex
    fixed = normalize_heartbeat_id_for_clob(raw)
    return fixed if fixed else uuid4().hex


def try_create_clob_client() -> Any | None:
    """
    Build authenticated py-clob-client ClobClient from environment (secrets stay env-only).

    TYREX_CLOB_HOST (default https://clob.polymarket.com)
    TYREX_CHAIN_ID (default 137)
    TYREX_PRIVATE_KEY (required) — or POLYMARKET_PK as fallback (common .env naming)
    TYREX_SIGNATURE_TYPE (default 0) — or POLYMARKET_SIGNATURE_TYPE as fallback
    TYREX_FUNDER (optional) — or POLYMARKET_FUNDER as fallback
    """
    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        log.warning("py-clob-client not installed; install tyrex-pm[live]")
        return None

    pk = (
        os.environ.get("TYREX_PRIVATE_KEY", "").strip()
        or os.environ.get("POLYMARKET_PK", "").strip()
    )
    if not pk:
        log.warning("TYREX_PRIVATE_KEY (or POLYMARKET_PK) not set; live CLOB disabled")
        return None

    host = os.environ.get("TYREX_CLOB_HOST", "https://clob.polymarket.com")
    chain_id = int(os.environ.get("TYREX_CHAIN_ID", "137"))
    sig_raw = (
        os.environ.get("TYREX_SIGNATURE_TYPE", "").strip()
        or os.environ.get("POLYMARKET_SIGNATURE_TYPE", "").strip()
        or "0"
    )
    sig_t = int(sig_raw)
    funder_raw = os.environ.get("TYREX_FUNDER") or os.environ.get("POLYMARKET_FUNDER") or ""
    funder = funder_raw.strip() or None

    client = ClobClient(host, chain_id=chain_id, key=pk, signature_type=sig_t, funder=funder)
    creds = client.create_or_derive_api_creds()
    if creds is None:
        log.error("Could not derive CLOB API credentials")
        return None
    client.set_api_creds(creds)
    return client


def resolve_positions_wallet_address(client: Any | None) -> str | None:
    """Return the address that holds outcome inventory on Polymarket.

    Order of precedence:

    1. ``TYREX_FUNDER`` / ``POLYMARKET_FUNDER`` (proxy/funder address) — required when
       ``signature_type != 0`` and the EOA itself does not custody positions.
    2. ``client.get_address()`` — the EOA, used when the bot trades from its own wallet.

    Returns ``None`` if neither is available; the positions REST loop is then disabled.
    """
    funder = (os.environ.get("TYREX_FUNDER") or os.environ.get("POLYMARKET_FUNDER") or "").strip()
    if funder:
        return funder
    if client is None:
        return None
    try:
        addr = client.get_address()
    except Exception:
        log.exception("py-clob-client get_address() failed; cannot enable positions REST")
        return None
    return str(addr) if addr else None
