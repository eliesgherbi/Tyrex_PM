"""Build `ClobClient` from process environment (see `.env` / v1.00 runbook)."""

from __future__ import annotations

import os

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

from tyrex_pm.config.loaders import RuntimeSettings


def build_clob_client_from_env(runtime: RuntimeSettings | None = None) -> ClobClient:
    pk = os.environ.get("POLYMARKET_PK")
    if not pk:
        raise RuntimeError("POLYMARKET_PK is not set (required for live execution)")

    if runtime is not None:
        host = runtime.clob_host
        chain_id = runtime.chain_id
    else:
        host = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
        chain_id = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
    funder = os.environ.get("POLYMARKET_FUNDER")

    if sig_type in (1, 2) and not funder:
        raise RuntimeError("POLYMARKET_FUNDER required for signature_type 1 or 2")

    api_key = os.environ.get("POLYMARKET_API_KEY")
    api_secret = os.environ.get("POLYMARKET_API_SECRET")
    passphrase = os.environ.get("POLYMARKET_PASSPHRASE")

    if (api_key or api_secret or passphrase) and not (
        api_key and api_secret and passphrase
    ):
        raise RuntimeError("L2 env incomplete: set all three of API_KEY, API_SECRET, PASSPHRASE")

    if api_key and api_secret and passphrase:
        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=passphrase,
        )
    else:
        temp = ClobClient(host, key=pk, chain_id=chain_id)
        creds = temp.create_or_derive_api_creds()

    kwargs: dict = {"host": host, "key": pk, "chain_id": chain_id, "creds": creds}
    if funder:
        kwargs["signature_type"] = sig_type
        kwargs["funder"] = funder
    else:
        kwargs["signature_type"] = 0

    return ClobClient(**kwargs)
