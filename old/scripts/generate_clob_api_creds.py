"""Generate Polymarket CLOB API credentials from the current V2 wallet env.

Run from the repo root:

    python scripts/generate_clob_api_creds.py

On success stdout contains exactly the three .env-ready lines to copy:

    POLYMARKET_API_KEY=...
    POLYMARKET_API_SECRET=...
    POLYMARKET_PASSPHRASE=...

Errors and explanatory text go to stderr.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def _load_dotenv() -> None:
    """Load repo-root .env without overwriting explicit shell exports."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_dotenv_fallback(env_path)
        return
    load_dotenv(env_path, override=False)


def _load_dotenv_fallback(path: Path) -> None:
    """Tiny .env parser used only when python-dotenv is unavailable."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_first(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _require_env(name: str, *aliases: str) -> str:
    value = _env_first(name, *aliases)
    if not value:
        all_names = ", ".join((name, *aliases))
        raise ValueError(f"missing required env var: {all_names}")
    return value


def _wallet_env() -> tuple[str, int, str | None]:
    pk = _require_env("POLYMARKET_PK", "TYREX_PRIVATE_KEY")
    sig_raw = _require_env("POLYMARKET_SIGNATURE_TYPE", "TYREX_SIGNATURE_TYPE")
    try:
        sig_t = int(sig_raw)
    except ValueError as exc:
        raise ValueError(
            "POLYMARKET_SIGNATURE_TYPE / TYREX_SIGNATURE_TYPE must be an integer "
            "(0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE, 3=POLY_1271)"
        ) from exc
    if sig_t not in (0, 1, 2, 3):
        raise ValueError(
            "POLYMARKET_SIGNATURE_TYPE / TYREX_SIGNATURE_TYPE must be one of "
            "0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE, 3=POLY_1271"
        )
    funder = _env_first("POLYMARKET_FUNDER", "TYREX_FUNDER") or None
    if sig_t != 0 and not funder:
        raise ValueError(
            "POLYMARKET_FUNDER (or TYREX_FUNDER) is required when signature_type "
            "is non-EOA. signature_type=1/2/3 signs with the private key but "
            "trades/custodies through the proxy/Safe/funder address."
        )
    return pk, sig_t, funder


def generate_creds() -> Any:
    """Call the V2 SDK API-key create/derive flow and return ApiCreds."""
    _load_dotenv()
    from py_clob_client_v2 import ClobClient
    from tyrex_pm.venue.polymarket.clob_env import (
        _derive_or_create_api_key,
        _resolve_builder_config,
        resolve_clob_host,
    )

    pk, sig_t, funder = _wallet_env()
    host = resolve_clob_host()
    chain_id = int(os.environ.get("TYREX_CHAIN_ID", "137"))
    client = ClobClient(
        host,
        chain_id=chain_id,
        key=pk,
        signature_type=sig_t,
        funder=funder,
        builder_config=_resolve_builder_config(),
    )
    creds = _derive_or_create_api_key(client)
    if creds is None:
        raise RuntimeError("V2 SDK returned no CLOB API credentials")
    return creds


def _print_env(creds: Any) -> None:
    api_key = getattr(creds, "api_key", "")
    api_secret = getattr(creds, "api_secret", "")
    passphrase = getattr(creds, "api_passphrase", "")
    if not api_key or not api_secret or not passphrase:
        raise RuntimeError("V2 SDK returned credentials with missing fields")
    print(f"POLYMARKET_API_KEY={api_key}")
    print(f"POLYMARKET_API_SECRET={api_secret}")
    print(f"POLYMARKET_PASSPHRASE={passphrase}")


def _explain_failure(exc: BaseException) -> str:
    return (
        f"ERROR: could not generate CLOB API credentials: {exc}\n\n"
        "Likely causes:\n"
        "- production auth bootstrap is still Cloudflare/WAF blocked for this request\n"
        "- POLYMARKET_SIGNATURE_TYPE does not match the wallet mode\n"
        "- POLYMARKET_FUNDER is missing or is not the proxy/Safe that holds funds\n"
        "- POLYMARKET_PK is not the signer for that wallet mode\n\n"
        "If this keeps failing, create CLOB API credentials from Polymarket's UI/API "
        "on an allowed network and paste POLYMARKET_API_KEY, "
        "POLYMARKET_API_SECRET, and POLYMARKET_PASSPHRASE into .env."
    )


def main() -> int:
    try:
        creds = generate_creds()
        _print_env(creds)
        return 0
    except Exception as exc:  # noqa: BLE001 - one-off operator script
        print(_explain_failure(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
