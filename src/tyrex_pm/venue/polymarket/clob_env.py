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

def v2_sdk_version() -> str | None:
    """Return the installed ``py-clob-client-v2`` package version, or ``None``.

    Lives here (not in the runtime layer) so non-venue modules can read the
    SDK version for evidence facts without importing ``py_clob_client_v2``
    directly — the import-isolation rule (see ``tests/test_v2_import_isolation.py``)
    forbids that. Wrapped in try/except so a missing-extras install or a future
    SDK that drops ``__version__`` does not crash live-attest evidence emission.
    """
    try:
        import py_clob_client_v2 as v2

        return getattr(v2, "__version__", None)
    except Exception:  # noqa: BLE001 — sdk version is best-effort evidence
        return None


DEFAULT_CLOB_HOST_V2 = "https://clob-v2.polymarket.com"
"""Pre-cutover (staging) V2 CLOB host.

V2 endpoints are first served from ``clob-v2.polymarket.com``. On cutover day
Polymarket promotes V2 to the canonical ``clob.polymarket.com`` and retires V1.
At that point this default is flipped to ``https://clob.polymarket.com``.
Operators can always override via ``TYREX_CLOB_HOST``.
"""

_BUILDER_CODE_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


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


def _resolve_builder_config() -> Any | None:
    """Build a ``BuilderConfig`` from env if both code and address are provided.

    Env:
      ``TYREX_BUILDER_CODE`` — bytes32 hex (``0x`` + 64 hex chars). Optional.
      ``TYREX_BUILDER_ADDRESS`` — 20-byte EOA hex. Required when builder code is set.

    Returns ``None`` when no builder code is configured. Raises ``ValueError`` on
    malformed input so misconfiguration fails fast at startup rather than silently
    submitting orders without builder attribution.
    """
    code_raw = (os.environ.get("TYREX_BUILDER_CODE") or "").strip()
    if not code_raw:
        return None
    if not _BUILDER_CODE_RE.match(code_raw):
        raise ValueError(
            "TYREX_BUILDER_CODE must be a 0x-prefixed 32-byte hex string "
            "(0x + 64 hex chars); got malformed value"
        )
    addr_raw = (os.environ.get("TYREX_BUILDER_ADDRESS") or "").strip()
    if not addr_raw:
        raise ValueError(
            "TYREX_BUILDER_CODE is set but TYREX_BUILDER_ADDRESS is missing; "
            "both are required to plumb a V2 builder config"
        )
    if not _ETH_ADDRESS_RE.match(addr_raw):
        raise ValueError(
            "TYREX_BUILDER_ADDRESS must be a 0x-prefixed 20-byte hex address"
        )
    from py_clob_client_v2 import BuilderConfig

    return BuilderConfig(builder_address=addr_raw, builder_code=code_raw)


def try_create_clob_client() -> Any | None:
    """
    Build an authenticated ``py-clob-client-v2`` ``ClobClient`` from environment.

    Secrets stay env-only; nothing is persisted.

    Env:
      TYREX_CLOB_HOST (default ``https://clob-v2.polymarket.com`` — pre-cutover
        V2 staging host; on cutover day this default is flipped to
        ``https://clob.polymarket.com``)
      TYREX_CHAIN_ID (default 137)
      TYREX_PRIVATE_KEY (required) — or ``POLYMARKET_PK`` as fallback
      TYREX_SIGNATURE_TYPE (default 0) — or ``POLYMARKET_SIGNATURE_TYPE`` fallback.
        V2 ``SignatureTypeV2`` int values: ``0=EOA``, ``1=POLY_PROXY``,
        ``2=POLY_GNOSIS_SAFE``, ``3=POLY_1271``.
      TYREX_FUNDER (optional) — or ``POLYMARKET_FUNDER`` as fallback
      TYREX_BUILDER_CODE / TYREX_BUILDER_ADDRESS (optional) — when set, both
        are required and plumbed via V2 ``BuilderConfig``.

    Phase 1 boundary: this function builds a V2 client, derives L2 API credentials
    via the V2 ``create_or_derive_api_key()`` path, and returns the configured
    client. It does *not* exercise V2 order submission (that belongs to Phase 2,
    in ``clob_bridge.py``).
    """
    try:
        from py_clob_client_v2 import ClobClient
    except ImportError:
        log.warning("py-clob-client-v2 not installed; install tyrex-pm[live]")
        return None

    pk = (
        os.environ.get("TYREX_PRIVATE_KEY", "").strip()
        or os.environ.get("POLYMARKET_PK", "").strip()
    )
    if not pk:
        log.warning("TYREX_PRIVATE_KEY (or POLYMARKET_PK) not set; live CLOB disabled")
        return None

    host = os.environ.get("TYREX_CLOB_HOST", DEFAULT_CLOB_HOST_V2)
    chain_id = int(os.environ.get("TYREX_CHAIN_ID", "137"))
    sig_raw = (
        os.environ.get("TYREX_SIGNATURE_TYPE", "").strip()
        or os.environ.get("POLYMARKET_SIGNATURE_TYPE", "").strip()
        or "0"
    )
    sig_t = int(sig_raw)
    funder_raw = os.environ.get("TYREX_FUNDER") or os.environ.get("POLYMARKET_FUNDER") or ""
    funder = funder_raw.strip() or None

    builder_config = _resolve_builder_config()

    client = ClobClient(
        host,
        chain_id=chain_id,
        key=pk,
        signature_type=sig_t,
        funder=funder,
        builder_config=builder_config,
    )
    creds = client.create_or_derive_api_key()
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
        log.exception("py-clob-client-v2 get_address() failed; cannot enable positions REST")
        return None
    return str(addr) if addr else None
