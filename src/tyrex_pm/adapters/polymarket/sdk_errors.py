"""Classify official ``polymarket-client`` exceptions into Tyrex error categories."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class PolymarketErrorCategory(str, Enum):
    TRANSIENT = "TRANSIENT"
    RATE_LIMIT = "RATE_LIMIT"
    REJECTED = "REJECTED"
    AUTH = "AUTH"
    USER_INPUT = "USER_INPUT"
    TRANSPORT = "TRANSPORT"
    UNEXPECTED = "UNEXPECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, kw_only=True)
class ClassifiedPolymarketError:
    category: PolymarketErrorCategory
    retryable: bool
    message: str
    original_type: str


def classify_polymarket_error(exc: BaseException) -> ClassifiedPolymarketError:
    """Map SDK / transport exceptions to stable Tyrex categories."""
    name = type(exc).__name__
    msg = str(exc)
    try:
        from polymarket import (
            RateLimitError,
            RequestRejectedError,
            TransportError,
            UnexpectedResponseError,
            UserInputError,
        )
        from polymarket import (
            TimeoutError as PmTimeoutError,
        )
        from polymarket.errors import ConnectionLostError, PolymarketError
    except ImportError:  # pragma: no cover
        return ClassifiedPolymarketError(
            category=PolymarketErrorCategory.UNKNOWN,
            retryable=False,
            message=msg,
            original_type=name,
        )

    if isinstance(exc, RateLimitError):
        return ClassifiedPolymarketError(
            category=PolymarketErrorCategory.RATE_LIMIT,
            retryable=True,
            message=msg,
            original_type=name,
        )
    if isinstance(exc, (PmTimeoutError, ConnectionLostError, TransportError)):
        return ClassifiedPolymarketError(
            category=PolymarketErrorCategory.TRANSIENT,
            retryable=True,
            message=msg,
            original_type=name,
        )
    if isinstance(exc, UserInputError):
        return ClassifiedPolymarketError(
            category=PolymarketErrorCategory.USER_INPUT,
            retryable=False,
            message=msg,
            original_type=name,
        )
    if isinstance(exc, RequestRejectedError):
        code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        retryable = code in {408, 429, 500, 502, 503, 504}
        cat = (
            PolymarketErrorCategory.AUTH
            if code in {401, 403}
            else PolymarketErrorCategory.REJECTED
        )
        return ClassifiedPolymarketError(
            category=cat,
            retryable=retryable,
            message=msg,
            original_type=name,
        )
    if isinstance(exc, UnexpectedResponseError):
        return ClassifiedPolymarketError(
            category=PolymarketErrorCategory.UNEXPECTED,
            retryable=False,
            message=msg,
            original_type=name,
        )
    if isinstance(exc, PolymarketError):
        return ClassifiedPolymarketError(
            category=PolymarketErrorCategory.TRANSPORT,
            retryable=True,
            message=msg,
            original_type=name,
        )
    return ClassifiedPolymarketError(
        category=PolymarketErrorCategory.UNKNOWN,
        retryable=False,
        message=msg,
        original_type=name,
    )


def raise_as_tyrex(exc: BaseException, *, prefix: str = "sdk") -> None:
    """Re-raise as RuntimeError with classified category (never returns)."""
    classified = classify_polymarket_error(exc)
    raise RuntimeError(
        f"{prefix}_{classified.category.value.lower()}:{classified.original_type}:{classified.message}"
    ) from exc


def is_retryable(exc: BaseException) -> bool:
    return classify_polymarket_error(exc).retryable


def sdk_exception_names() -> tuple[type[BaseException], ...]:
    """Import SDK exception types for isinstance checks in tests."""
    try:
        from polymarket import (
            RateLimitError,
            RequestRejectedError,
            TransportError,
            UnexpectedResponseError,
            UserInputError,
        )
        from polymarket import (
            TimeoutError as PmTimeoutError,
        )
        from polymarket.errors import ConnectionLostError, PolymarketError

        return (
            RateLimitError,
            RequestRejectedError,
            PmTimeoutError,
            TransportError,
            UnexpectedResponseError,
            UserInputError,
            ConnectionLostError,
            PolymarketError,
        )
    except ImportError:  # pragma: no cover
        return ()


def describe_error(exc: BaseException) -> dict[str, Any]:
    c = classify_polymarket_error(exc)
    return {
        "category": c.category.value,
        "retryable": c.retryable,
        "message": c.message,
        "original_type": c.original_type,
    }


# Categories that may warrant operator VPN/DNS guidance (transport-class only).
_VPN_HINT_CATEGORIES = frozenset(
    {
        PolymarketErrorCategory.TRANSPORT,
        PolymarketErrorCategory.TRANSIENT,
    }
)

# Domain / identity failure markers — never map to VPN hint by themselves.
_DOMAIN_ERROR_MARKERS = (
    "MARKET_SLUG_MISMATCH",
    "MARKET_SLUG_MALFORMED",
    "MARKET_WINDOW_ALIGNMENT_INVALID",
    "MARKET_WINDOW_END_MISMATCH",
    "MARKET_OUTCOME_BINDING_INVALID",
    "MARKET_IDENTIFIERS_MISSING",
    "STALE_DISCOVERY_PAYLOAD",
    "MARKET_DURATION_MISMATCH",
    "TITLE_TIME_MISMATCH",
    "INVALID_MARKET_WINDOW",
    "MARKET_NOT_ACCEPTING_ORDERS",
    "WRONG_WINDOW",
    "INCOMPLETE_GAMMA",
    "MARKET_RULE_SOURCE_MISMATCH",
    "OUTCOME_MAP_REJECTED",
    "NO_PTB_SEAL",
    "PTB_NOT_STARTED",
    "PTB_ABSENT",
    "ADAPTER_CONTRACT",
    "AUTHORIZATION",
    "USER_STREAM_AUTH",
    "USER_STREAM_PROTOCOL",
    "UNEXPECTED_OPEN_ORDER",
)

# Explicit transport-class markers (taxonomy / SDK category prefixes preferred).
_TRANSPORT_ERROR_MARKERS = (
    "sdk_transport:",
    "sdk_transient:",
    "failure_kind=transport",
    "failure_kind:transport",
    "public_clob_unreachable",
    "hint_check_vpn_or_dns",
    "getaddrinfo",
    "nameresolution",
    "certificate verify",
    "sslcertverificationerror",
    "connection refused",
    "connection reset",
    "network unreachable",
    "errno 11001",  # Windows DNS
    "errno -2",  # POSIX DNS
    "timed out",
)


def is_transport_class_failure(exc: BaseException) -> bool:
    """True only for proven transport / connectivity-class SDK failures."""
    return classify_polymarket_error(exc).category in _VPN_HINT_CATEGORIES


def vpn_hint_from_error_texts(errors: list[str] | tuple[str, ...] | None) -> bool:
    """VPN/DNS guidance only for transport-class failures.

    Market-identity, PTB-domain, auth, and adapter-contract errors never set
    ``vpn_hint`` unless a separate transport-class row is also present.
    """
    if not errors:
        return False
    saw_transport = False
    for raw in errors:
        text = str(raw)
        upper = text.upper()
        lower = text.lower()
        domain = any(marker in upper for marker in _DOMAIN_ERROR_MARKERS)
        transport = any(marker in lower for marker in _TRANSPORT_ERROR_MARKERS)
        if "sdk_transport:" in lower or "sdk_transient:" in lower:
            transport = True
        # Domain identity rows never count as VPN evidence unless they also
        # carry an explicit transport-class marker (e.g. discovery DNS failure).
        if domain and not transport:
            continue
        if transport:
            saw_transport = True
    return saw_transport


_PRIMARY_MARKET_CODES = (
    "MARKET_SLUG_MISMATCH",
    "MARKET_SLUG_MALFORMED",
    "MARKET_WINDOW_ALIGNMENT_INVALID",
    "MARKET_WINDOW_END_MISMATCH",
    "MARKET_OUTCOME_BINDING_INVALID",
    "MARKET_IDENTIFIERS_MISSING",
    "STALE_DISCOVERY_PAYLOAD",
    "MARKET_DURATION_MISMATCH",
    "TITLE_TIME_MISMATCH",
    "INVALID_MARKET_WINDOW",
    "MARKET_NOT_ACCEPTING_ORDERS",
    "market_rule_source_mismatch",
)


def primary_blocker_from_compose_errors(
    errors: list[str] | tuple[str, ...] | None,
) -> tuple[str | None, str | None]:
    """Return ``(primary_abort_code, downstream_effect)`` from compose errors.

    Discovery / market-identity failures are primary; ``PTB_NOT_STARTED`` is the
    downstream effect when sealing never began.
    """
    if not errors:
        return None, None
    for raw in errors:
        text = str(raw)
        for code in _PRIMARY_MARKET_CODES:
            if code in text:
                return code, "PTB_NOT_STARTED"
        if "discovery:" in text.lower() or "wrong_window" in text:
            return "discovery_failed", "PTB_NOT_STARTED"
    return None, None
