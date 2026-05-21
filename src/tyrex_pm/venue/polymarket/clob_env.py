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


DEFAULT_CLOB_HOST_V2 = "https://clob.polymarket.com"
"""Post-cutover canonical V2 CLOB host.

During the migration window V2 was validated on ``clob-v2.polymarket.com``.
After Polymarket's production cutover, V2 lives at ``clob.polymarket.com`` and
the old transition host redirects/301s auth endpoints. Operators can still set
``TYREX_CLOB_HOST`` explicitly, but the historical transition host is rewritten
with a warning by :func:`resolve_clob_host`.
"""

PRE_CUTOVER_CLOB_HOST_V2 = "https://clob-v2.polymarket.com"
"""Historical V2 transition host; kept only to normalize stale operator env."""

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


def _resolve_env_api_creds() -> Any | None:
    """Return pre-created CLOB API credentials from env, if fully configured.

    These are Polymarket CLOB API credentials, not Builder Relayer credentials.
    Prefer them when present so production bootstrap does not need to call the
    API-key creation endpoint, which may be Cloudflare-blocked post-cutover.
    """
    api_key = (os.environ.get("POLYMARKET_API_KEY") or "").strip()
    api_secret = (os.environ.get("POLYMARKET_API_SECRET") or "").strip()
    passphrase = (os.environ.get("POLYMARKET_PASSPHRASE") or "").strip()
    present = [bool(api_key), bool(api_secret), bool(passphrase)]
    if not any(present):
        return None
    if not all(present):
        raise ValueError(
            "CLOB API credentials are partially configured; set all three "
            "POLYMARKET_API_KEY, POLYMARKET_API_SECRET, and POLYMARKET_PASSPHRASE "
            "or unset all three to derive credentials from POLYMARKET_PK"
        )
    from py_clob_client_v2 import ApiCreds

    return ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=passphrase,
    )


def _api_creds_usable(creds: Any) -> bool:
    if creds is None:
        return False
    key = getattr(creds, "api_key", None)
    return isinstance(key, str) and bool(key)


def _derive_or_create_api_key(client: Any) -> Any | None:
    """Return L2 ``ApiCreds`` using GET derive first, then POST create, then GET derive again.

    ``py_clob_client_v2.ClobClient.create_or_derive_api_key`` always **POSTs**
    ``/auth/api-key`` first. When a key already exists, Polymarket responds
    **400** ``{"error":"Could not create api key"}``; the SDK HTTP helper logs
    that at **ERROR** before catching the exception and falling back to **GET**
    ``/auth/derive-api-key``. Tyrex prefers **derive → create → derive** so
    existing keys avoid the noisy failed create — same outcomes as the SDK for
    typical accounts.
    """
    try:
        creds = client.derive_api_key()
        if _api_creds_usable(creds):
            return creds
    except Exception:  # noqa: BLE001 — mirror SDK create_or_derive broad catch
        pass
    try:
        creds = client.create_api_key()
        if _api_creds_usable(creds):
            return creds
    except Exception:
        pass
    try:
        creds = client.derive_api_key()
        if _api_creds_usable(creds):
            return creds
    except Exception:
        pass
    return None


def resolve_clob_host() -> str:
    """Resolve the CLOB REST host for the post-cutover V2 runtime.

    ``TYREX_CLOB_HOST`` remains an explicit operator override, but the old
    pre-cutover host is no longer a valid production target after Polymarket's
    V2 cutover. Rewriting that exact value avoids an immediate SDK bootstrap
    301 while logging loudly enough for run logs / facts to explain why the
    host changed.
    """
    raw = (os.environ.get("TYREX_CLOB_HOST") or DEFAULT_CLOB_HOST_V2).strip()
    host = raw.rstrip("/")
    if host == PRE_CUTOVER_CLOB_HOST_V2:
        log.warning(
            "TYREX_CLOB_HOST=%s is the pre-cutover V2 transition host and now "
            "redirects auth endpoints; using post-cutover production host %s",
            PRE_CUTOVER_CLOB_HOST_V2,
            DEFAULT_CLOB_HOST_V2,
        )
        return DEFAULT_CLOB_HOST_V2
    return host


def try_create_clob_client() -> Any | None:
    """
    Build an authenticated ``py-clob-client-v2`` ``ClobClient`` from environment.

    Secrets stay env-only; nothing is persisted.

    Env:
      TYREX_CLOB_HOST (default ``https://clob.polymarket.com`` — post-cutover
        V2 production host; stale ``https://clob-v2.polymarket.com`` values are
        rewritten with a warning)
      TYREX_CHAIN_ID (default 137)
      TYREX_PRIVATE_KEY (required) — or ``POLYMARKET_PK`` as fallback
      TYREX_SIGNATURE_TYPE (default 0) — or ``POLYMARKET_SIGNATURE_TYPE`` fallback.
        V2 ``SignatureTypeV2`` int values: ``0=EOA``, ``1=POLY_PROXY``,
        ``2=POLY_GNOSIS_SAFE``, ``3=POLY_1271``.
      TYREX_FUNDER (optional) — or ``POLYMARKET_FUNDER`` as fallback
      TYREX_BUILDER_CODE / TYREX_BUILDER_ADDRESS (optional) — when set, both
        are required and plumbed via V2 ``BuilderConfig``.
      POLYMARKET_API_KEY / POLYMARKET_API_SECRET / POLYMARKET_PASSPHRASE
        (optional) — pre-created CLOB API credentials. When all three are set,
        they are used directly and the SDK's API-key creation/derive endpoint
        is not called.

    This function builds a V2 client, configures L2 API credentials from env or
    via :func:`_derive_or_create_api_key` (derive-first; avoids spurious
    ``create`` **400** logs from the stock SDK helper), and returns the
    configured client. It does *not* exercise V2 order submission (that belongs
    to ``clob_bridge.py``).
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

    host = resolve_clob_host()
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
    env_creds = _resolve_env_api_creds()

    client = ClobClient(
        host,
        chain_id=chain_id,
        key=pk,
        signature_type=sig_t,
        funder=funder,
        builder_config=builder_config,
    )
    creds = env_creds if env_creds is not None else _derive_or_create_api_key(client)
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
