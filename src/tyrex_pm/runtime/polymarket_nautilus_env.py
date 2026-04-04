"""Ensure Polymarket L2 credentials exist for Nautilus live factories.

**Package-source-confirmed:** ``nautilus_trader.adapters.polymarket.factories`` reads
``POLYMARKET_API_KEY`` / ``SECRET`` / ``PASSPHRASE`` from the environment (not from PK).

This mirrors ``scripts/spike_nautilus_polymarket_exec._ensure_polymarket_l2_env_for_nautilus_factory``
without importing the spike script into production paths.

``CopyStrategy`` / risk code must **not** call this — only ``guru_compose`` when registering
Nautilus live clients.
"""

from __future__ import annotations

import os

from py_clob_client.client import ClobClient


def ensure_polymarket_l2_env_from_pk_if_missing() -> None:
    """
    If L2 triple is missing but ``POLYMARKET_PK`` is set, derive API creds via py-clob
    and set ``os.environ`` defaults (non-overriding), matching ``clob_factory`` / verify script.
    """
    if os.environ.get("POLYMARKET_API_KEY") and os.environ.get("POLYMARKET_API_SECRET"):
        if os.environ.get("POLYMARKET_PASSPHRASE"):
            return
    pk = os.environ.get("POLYMARKET_PK")
    if not pk:
        return

    host = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
    chain_id = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))
    temp = ClobClient(host, key=pk, chain_id=chain_id)
    creds = temp.create_or_derive_api_creds()
    os.environ.setdefault("POLYMARKET_API_KEY", creds.api_key)
    os.environ.setdefault("POLYMARKET_API_SECRET", creds.api_secret)
    os.environ.setdefault("POLYMARKET_PASSPHRASE", creds.api_passphrase)
