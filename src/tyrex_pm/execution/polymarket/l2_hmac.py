"""L2 HMAC helpers aligned with official ``py_clob_client_v2.signing.hmac``.

Structural logging only — never log secrets, signatures, or addresses.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any

from tyrex_pm.execution.polymarket.auth import L2Credentials


@dataclass(frozen=True)
class HmacStructureReport:
    method: str
    path_shape: str
    query_present: bool
    body_present: bool
    timestamp_digits: int
    secret_base64_valid: bool
    signature_length: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path_shape": self.path_shape,
            "query_present": self.query_present,
            "body_present": self.body_present,
            "timestamp_digits": self.timestamp_digits,
            "secret_base64_valid": self.secret_base64_valid,
            "signature_length": self.signature_length,
        }


def secret_base64_valid(secret: str) -> bool:
    try:
        base64.urlsafe_b64decode(secret)
        return True
    except Exception:  # noqa: BLE001
        return False


def build_hmac_signature(
    secret: str,
    timestamp: str | int,
    method: str,
    request_path: str,
    body: str | None = None,
) -> str:
    """Match official V2 ``build_hmac_signature`` (URL-safe base64 HMAC-SHA256)."""
    base64_secret = base64.urlsafe_b64decode(secret)
    message = str(timestamp) + str(method) + str(request_path)
    if body:
        message += str(body).replace("'", '"')
    digest = hmac.new(base64_secret, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8")


def build_l2_headers(
    creds: L2Credentials,
    *,
    method: str,
    request_path: str,
    body: str | None = None,
    timestamp: int | None = None,
) -> tuple[dict[str, str], HmacStructureReport]:
    """Build POLY_* headers. ``POLY_ADDRESS`` is always the signer (creds.address)."""
    ts = int(time.time()) if timestamp is None else int(timestamp)
    ts_s = str(ts)
    # Official SDK passes method as provided by RequestArgs (typically uppercase).
    method_canon = method.upper()
    sig = build_hmac_signature(creds.secret, ts_s, method_canon, request_path, body)
    headers = {
        "POLY_ADDRESS": creds.address,
        "POLY_SIGNATURE": sig,
        "POLY_TIMESTAMP": ts_s,
        "POLY_API_KEY": creds.api_key,
        "POLY_PASSPHRASE": creds.passphrase,
        "Content-Type": "application/json",
    }
    report = HmacStructureReport(
        method=method_canon,
        path_shape=request_path.split("?", 1)[0],
        query_present="?" in request_path,
        body_present=bool(body),
        timestamp_digits=len(ts_s),
        secret_base64_valid=secret_base64_valid(creds.secret),
        signature_length=len(sig),
    )
    return headers, report
