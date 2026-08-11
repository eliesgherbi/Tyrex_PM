"""Classify official ``polymarket-client`` exceptions into Tyrex error categories."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any


class PolymarketErrorCategory(str, Enum):
    TRANSIENT = "TRANSIENT"
    RATE_LIMIT = "RATE_LIMIT"
    REJECTED = "REJECTED"
    ENGINE_RESTART = "ENGINE_RESTART"  # HTTP 425 matching-engine restart
    AUTH = "AUTH"
    USER_INPUT = "USER_INPUT"
    TRANSPORT = "TRANSPORT"
    UNCERTAIN_DISPATCH = "UNCERTAIN_DISPATCH"
    UNEXPECTED = "UNEXPECTED"
    UNKNOWN = "UNKNOWN"


class PolymarketOperation(str, Enum):
    """Operation context controls whether a transport failure is retryable."""

    READ = "READ"
    PREPARE = "PREPARE"
    DISPATCH = "DISPATCH"


@dataclass(frozen=True, kw_only=True)
class ClassifiedPolymarketError:
    category: PolymarketErrorCategory
    retryable: bool
    message: str
    original_type: str
    status: int | None = None
    retry_after: float | None = None


def classify_polymarket_error(
    exc: BaseException,
    *,
    operation: PolymarketOperation = PolymarketOperation.READ,
) -> ClassifiedPolymarketError:
    """Map SDK / transport exceptions to stable Tyrex categories.

    Prefer typed SDK fields (``RequestRejectedError.status``) over string parsing.
    HTTP 425 is treated as a temporary matching-engine restart (bounded backoff).
    Timeouts/connection loss after possible dispatch are ``UNCERTAIN_DISPATCH``
    (reconcile before any mutation retry — never blind POST retry).
    """
    name = type(exc).__name__
    msg = str(exc)
    if isinstance(exc, asyncio.CancelledError) and operation is PolymarketOperation.DISPATCH:
        return ClassifiedPolymarketError(
            category=PolymarketErrorCategory.UNCERTAIN_DISPATCH,
            retryable=False,
            message=msg or "dispatch task cancelled before a response was observed",
            original_type=name,
        )
    # ``asyncio.timeout`` raises the built-in TimeoutError.  Classify it even
    # when SDK exception imports are unavailable (for example in isolated
    # domain tests), because the operation boundary is already known here.
    if isinstance(exc, TimeoutError):
        return ClassifiedPolymarketError(
            category=(
                PolymarketErrorCategory.TRANSPORT
                if operation is not PolymarketOperation.DISPATCH
                else PolymarketErrorCategory.UNCERTAIN_DISPATCH
            ),
            retryable=operation is not PolymarketOperation.DISPATCH,
            message=msg,
            original_type=name,
        )
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
        retry_after = getattr(exc, "retry_after", None)
        return ClassifiedPolymarketError(
            category=PolymarketErrorCategory.RATE_LIMIT,
            retryable=True,
            message=msg,
            original_type=name,
            retry_after=None if retry_after is None else float(retry_after),
        )
    if isinstance(exc, (PmTimeoutError, ConnectionLostError, TransportError)):
        if operation is not PolymarketOperation.DISPATCH:
            return ClassifiedPolymarketError(
                category=PolymarketErrorCategory.TRANSPORT,
                retryable=True,
                message=msg,
                original_type=name,
            )
        return ClassifiedPolymarketError(
            category=PolymarketErrorCategory.UNCERTAIN_DISPATCH,
            retryable=False,  # reconcile first; do not blind-retry POST
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
        code_raw = getattr(exc, "status", None)
        if code_raw is None:
            code_raw = getattr(exc, "status_code", None)
        code = int(code_raw) if code_raw is not None else None
        retry_after = getattr(exc, "retry_after", None)
        if code == 425:
            return ClassifiedPolymarketError(
                category=PolymarketErrorCategory.ENGINE_RESTART,
                retryable=True,
                message=msg,
                original_type=name,
                status=425,
                retry_after=None if retry_after is None else float(retry_after),
            )
        retryable = code in {408, 429, 500, 502, 503, 504}
        cat = (
            PolymarketErrorCategory.AUTH if code in {401, 403} else PolymarketErrorCategory.REJECTED
        )
        return ClassifiedPolymarketError(
            category=cat,
            retryable=retryable,
            message=msg,
            original_type=name,
            status=code,
            retry_after=None if retry_after is None else float(retry_after),
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
            retryable=False,
            message=msg,
            original_type=name,
        )
    return ClassifiedPolymarketError(
        category=PolymarketErrorCategory.UNKNOWN,
        retryable=False,
        message=msg,
        original_type=name,
    )


def is_engine_restart(exc: BaseException) -> bool:
    return classify_polymarket_error(exc).category is PolymarketErrorCategory.ENGINE_RESTART


def is_uncertain_dispatch(exc: BaseException) -> bool:
    return (
        classify_polymarket_error(exc, operation=PolymarketOperation.DISPATCH).category
        is PolymarketErrorCategory.UNCERTAIN_DISPATCH
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


def describe_error(
    exc: BaseException,
    *,
    operation: PolymarketOperation = PolymarketOperation.READ,
) -> dict[str, Any]:
    c = classify_polymarket_error(exc, operation=operation)
    return {
        "category": c.category.value,
        "retryable": c.retryable,
        "message": c.message,
        "original_type": c.original_type,
        "status": c.status,
        "retry_after": c.retry_after,
    }


# Categories that may warrant operator VPN/DNS guidance (transport-class only).
_VPN_HINT_CATEGORIES = frozenset(
    {
        PolymarketErrorCategory.TRANSPORT,
        PolymarketErrorCategory.TRANSIENT,
        PolymarketErrorCategory.UNCERTAIN_DISPATCH,
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
