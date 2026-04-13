"""
Dynamic guru ``token_id`` → Nautilus ``BinaryOption`` + ``Cache`` activation.

**Resolution (public HTTP, Tyrex-owned orchestration):**
  #. Gamma Get Markets with ``clob_token_ids`` → ``conditionId`` (**Docs-confirmed** Gamma API;
     Tyrex uses the documented filter shape from **Package-source-confirmed**
     ``nautilus_trader.adapters.polymarket.common.gamma_markets.build_markets_query``).
  #. py-clob ``get_market(condition_id)`` for CLOB-shaped market dict (**Repo-confirmed**
     ``PolymarketInstrumentProvider`` uses the same call).
  #. ``parse_polymarket_instrument`` (**Package-source-confirmed** ``common.parsing``).

**Wallet warmup** may retry the CLOB+parse leg using ``conditionId`` from the Data API
``/positions`` row when Gamma lookup fails — still the **same** ``get_market`` +
``parse_polymarket_instrument`` path; **no** alternate ``InstrumentId`` construction.

**Activation:**
  #. ``Cache.add_instrument`` + ``add_currency`` when needed — **public** ``Cache`` API
     (**Package-source-confirmed**; same primitive ``PolymarketDataClient`` uses for instruments).

This does **not** patch Nautilus clients. Exec client **``_maintain_active_market``** still runs on
submit (**Package-source-confirmed**). Tyrex never owns a second instrument truth store beyond
what enters ``Cache``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, NamedTuple

import httpx
from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.adapters.polymarket.common.parsing import parse_polymarket_instrument
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_token_id
from nautilus_trader.cache.cache import Cache
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import BinaryOption, Instrument
from py_clob_client.client import ClobClient

from tyrex_pm.config.loaders import RuntimeSettings

if TYPE_CHECKING:
    pass

_LOG = logging.getLogger(__name__)


class WalletPositionResolveOutcome(NamedTuple):
    """
    Result of :meth:`GuruInstrumentDynamicController.resolve_and_activate_wallet_position`.

    ``detail`` is a stable machine-readable code (empty on success). ``message`` is a
    short human/operator fragment (from :class:`GuruInstrumentResolveError` or ``None``).
    """

    instrument: Instrument | None
    detail: str
    message: str | None


# When Gamma-first resolution fails with one of these codes, wallet warmup may retry via
# ``conditionId`` from the same Data API row (still ``get_market`` + ``parse_polymarket_instrument``).
_WALLET_CONDITION_FALLBACK_DETAILS: frozenset[str] = frozenset(
    {
        "gamma_empty",
        "gamma_bad_shape",
        "gamma_no_condition",
        "clob_token_missing",
        "clob_error_string",
        "clob_bad_type",
    }
)


class GuruInstrumentResolveError(Exception):
    """Gamma/CLOB/Parse chain failed for a guru ``token_id``."""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


def _fetch_gamma_market_row(
    token_id: str,
    *,
    base_url: str,
    timeout: float,
) -> dict[str, object]:
    url = f"{base_url.rstrip('/')}/markets"
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(
            url,
            params={"clob_token_ids": str(token_id), "limit": 1},
        )
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, list) or not data:
        raise GuruInstrumentResolveError(
            f"Gamma returned no market for clob_token_ids={token_id!r}",
            detail="gamma_empty",
        )
    row = data[0]
    if not isinstance(row, dict):
        raise GuruInstrumentResolveError(
            f"Unexpected Gamma row type: {type(row).__name__}",
            detail="gamma_bad_shape",
        )
    return row


def _clob_market_dict(clob_client: ClobClient, condition_id: str) -> dict[str, object]:
    raw = clob_client.get_market(str(condition_id))
    if isinstance(raw, str):
        raise GuruInstrumentResolveError(
            f"CLOB get_market returned error string: {raw[:240]}",
            detail="clob_error_string",
        )
    if not isinstance(raw, dict):
        raise GuruInstrumentResolveError(
            f"CLOB get_market unexpected type: {type(raw).__name__}",
            detail="clob_bad_type",
        )
    return raw


def _binary_option_from_clob_market_dict(
    token_id: str,
    raw: dict[str, object],
) -> BinaryOption:
    """
    Match ``token_id`` to an outcome in ``raw['tokens']`` and run **Nautilus**
    ``parse_polymarket_instrument`` (single instrument identity path).
    """
    outcome: str | None = None
    for t in raw.get("tokens") or []:
        if not isinstance(t, dict):
            continue
        if str(t.get("token_id")) == str(token_id):
            o = t.get("outcome")
            outcome = str(o) if o is not None else None
            break
    if outcome is None:
        raise GuruInstrumentResolveError(
            f"token_id {token_id!r} not listed in CLOB market tokens",
            detail="clob_token_missing",
        )
    try:
        return parse_polymarket_instrument(
            market_info=raw,
            token_id=str(token_id),
            outcome=outcome,
            ts_init=time.time_ns(),
        )
    except Exception as exc:
        raise GuruInstrumentResolveError(
            f"parse_polymarket_instrument failed: {type(exc).__name__}",
            detail="parse_failed",
        ) from exc


def resolve_binary_option_for_condition_and_token(
    condition_id: str,
    token_id: str,
    clob_client: ClobClient,
) -> BinaryOption:
    """
    CLOB + parse only (no Gamma). ``InstrumentId`` matches Nautilus
    ``{condition_id}-{token_id}.POLYMARKET``.
    """
    raw = _clob_market_dict(clob_client, str(condition_id))
    return _binary_option_from_clob_market_dict(str(token_id), raw)


def resolve_binary_option_for_clob_token(
    token_id: str,
    clob_client: ClobClient,
    *,
    gamma_base_url: str,
    http_timeout: float,
) -> BinaryOption:
    """
    Build a :class:`~nautilus_trader.model.instruments.BinaryOption` for ``outcome token_id``.

    Raises :class:`GuruInstrumentResolveError` when any HTTP/parse step fails.
    """
    gamma_row = _fetch_gamma_market_row(
        token_id,
        base_url=gamma_base_url,
        timeout=http_timeout,
    )
    condition_id = gamma_row.get("conditionId")
    if not condition_id:
        raise GuruInstrumentResolveError(
            "Gamma market row missing conditionId",
            detail="gamma_no_condition",
        )

    raw = _clob_market_dict(clob_client, str(condition_id))
    return _binary_option_from_clob_market_dict(str(token_id), raw)


def resolve_binary_option_for_wallet_warmup(
    token_id: str,
    clob_client: ClobClient,
    *,
    gamma_base_url: str,
    http_timeout: float,
    row_condition_id: str | None,
) -> BinaryOption:
    """
    Gamma-first resolution, then **optional** retry using ``row_condition_id`` from Data API
    ``/positions`` (``package-source`` Nautilus uses ``conditionId`` + ``asset`` for the same API).

    Does **not** catch ``parse_failed`` fallback — incompatible market types must surface clearly.
    """
    try:
        return resolve_binary_option_for_clob_token(
            token_id,
            clob_client,
            gamma_base_url=gamma_base_url,
            http_timeout=http_timeout,
        )
    except GuruInstrumentResolveError as exc:
        detail = exc.detail or ""
        cond = (row_condition_id or "").strip()
        if not cond or detail not in _WALLET_CONDITION_FALLBACK_DETAILS:
            raise
        return resolve_binary_option_for_condition_and_token(
            cond,
            str(token_id),
            clob_client,
        )


class CacheInstrumentActivator:
    """
    Controlled addition of dynamically resolved instruments to ``Cache``.

    Caps **new** activations per process (session) to limit WS proliferation
    (**Issue-confirmed** operational concern per ``polymarket_cache_seeding_decision.md``).
    """

    def __init__(self, cache: Cache, *, max_new_activations: int) -> None:
        if max_new_activations < 0:
            raise ValueError("max_new_activations must be >= 0")
        self._cache = cache
        self._max_new_activations = max_new_activations
        self._lock = threading.Lock()
        self._new_activations_used = 0

    def try_add_instrument(self, instrument: BinaryOption) -> tuple[bool, str]:
        """
        Return ``(ok, reason)``.

        If the instrument is already in ``Cache``, returns ``(True, "already_cached")`` without
        consuming the activation budget.
        """
        existing = self._cache.instrument(instrument.id)
        if existing is not None:
            return True, "already_cached"

        with self._lock:
            if self._new_activations_used >= self._max_new_activations:
                return False, "activation_cap"
            self._cache.add_currency(instrument.quote_currency)
            self._cache.add_instrument(instrument)
            self._new_activations_used += 1
        return True, "activated"

    def force_add_instrument(self, instrument: BinaryOption) -> tuple[bool, str]:
        """
        Insert without consuming ``max_new_activations`` (follower ``/positions`` warmup only).
        """
        existing = self._cache.instrument(instrument.id)
        if existing is not None:
            return True, "already_cached"
        with self._lock:
            self._cache.add_currency(instrument.quote_currency)
            self._cache.add_instrument(instrument)
        return True, "activated"


class GuruInstrumentDynamicController:
    """
    Runtime bridge: resolve + activate for framework guru submit (**not** used from strategy).
    """

    def __init__(
        self,
        cache: Cache,
        clob_client: ClobClient,
        runtime: RuntimeSettings,
    ) -> None:
        self._cache = cache
        self._clob = clob_client
        self._runtime = runtime
        self._activator = CacheInstrumentActivator(
            cache,
            max_new_activations=runtime.polymarket_dynamic_max_activations,
        )

    def resolve_and_activate(self, token_id: str) -> tuple[Instrument | None, str]:
        """
        Resolve guru ``token_id`` and ensure the instrument is in ``Cache``.

        On resolution failure returns ``(None, <classified_detail>)`` where ``detail`` is
        e.g. ``gamma_empty``, ``parse_failed``, ``http_error`` — never a vague ``resolve_failed``
        when :class:`GuruInstrumentResolveError` carried a code.
        """
        tid = str(token_id)
        for cached in self._cache.instruments(venue=Venue(POLYMARKET)):
            if str(get_polymarket_token_id(cached.id)) == tid:
                return cached, ""

        try:
            inst = resolve_binary_option_for_clob_token(
                tid,
                self._clob,
                gamma_base_url=self._runtime.polymarket_gamma_base_url,
                http_timeout=float(self._runtime.polymarket_gamma_http_timeout_seconds),
            )
        except GuruInstrumentResolveError as exc:
            d = exc.detail or "resolve_unspecified"
            _LOG.info(
                "event=guru_instrument_resolve_fail component=guru_instrument_dynamic "
                "token_id=%s detail=%s msg=%s",
                tid[:24],
                d,
                str(exc)[:200],
            )
            return None, d
        except httpx.HTTPError:
            _LOG.warning(
                "event=guru_instrument_resolve_fail component=guru_instrument_dynamic "
                "token_id=%s detail=http_error",
                tid[:24],
                exc_info=True,
            )
            return None, "http_error"
        except (OSError, ValueError, TypeError, KeyError):
            _LOG.warning(
                "event=guru_instrument_resolve_fail component=guru_instrument_dynamic "
                "token_id=%s detail=unexpected_error",
                tid[:24],
                exc_info=True,
            )
            return None, "unexpected_error"

        ok, _reason = self._activator.try_add_instrument(inst)
        if not ok:
            return None, "activation_cap"
        cached = self._cache.instrument(inst.id)
        if cached is None:
            return None, "cache_inconsistent"
        return cached, ""

    def resolve_and_activate_wallet_position(
        self,
        token_id: str,
        *,
        row_condition_id: str | None = None,
    ) -> WalletPositionResolveOutcome:
        """
        Resolve ``token_id`` into ``Cache`` for **follower** holdings — **does not** consume
        ``polymarket_dynamic_max_activations`` (wallet ``/positions`` warmup only).

        ``row_condition_id`` should be the Data API ``conditionId`` when available; enables
        CLOB+parse retry without a second instrument naming scheme.
        """
        tid = str(token_id)
        for cached in self._cache.instruments(venue=Venue(POLYMARKET)):
            if str(get_polymarket_token_id(cached.id)) == tid:
                return WalletPositionResolveOutcome(cached, "", None)

        try:
            inst = resolve_binary_option_for_wallet_warmup(
                tid,
                self._clob,
                gamma_base_url=self._runtime.polymarket_gamma_base_url,
                http_timeout=float(self._runtime.polymarket_gamma_http_timeout_seconds),
                row_condition_id=row_condition_id,
            )
        except GuruInstrumentResolveError as exc:
            d = exc.detail or "resolve_unspecified"
            msg = str(exc)
            if len(msg) > 240:
                msg = msg[:237] + "..."
            # Row-level WARNING is emitted by guru_cache_warmup (single operator-facing line).
            _LOG.debug(
                "event=guru_instrument_resolve_fail component=guru_instrument_dynamic "
                "context=wallet_warmup token_id=%s detail=%s msg=%s",
                tid[:24],
                d,
                msg,
            )
            return WalletPositionResolveOutcome(None, d, msg)
        except httpx.HTTPError:
            _LOG.warning(
                "event=guru_instrument_resolve_fail component=guru_instrument_dynamic "
                "context=wallet_warmup token_id=%s detail=http_error",
                tid[:24],
                exc_info=True,
            )
            return WalletPositionResolveOutcome(None, "http_error", None)
        except (OSError, ValueError, TypeError, KeyError):
            _LOG.warning(
                "event=guru_instrument_resolve_fail component=guru_instrument_dynamic "
                "context=wallet_warmup token_id=%s detail=unexpected_error",
                tid[:24],
                exc_info=True,
            )
            return WalletPositionResolveOutcome(None, "unexpected_error", None)

        ok, reason = self._activator.force_add_instrument(inst)
        if not ok:
            return WalletPositionResolveOutcome(None, reason, None)
        cached = self._cache.instrument(inst.id)
        if cached is None:
            return WalletPositionResolveOutcome(None, "cache_inconsistent", None)
        return WalletPositionResolveOutcome(cached, "", None)


__all__ = [
    "CacheInstrumentActivator",
    "GuruInstrumentDynamicController",
    "GuruInstrumentResolveError",
    "WalletPositionResolveOutcome",
    "resolve_binary_option_for_clob_token",
    "resolve_binary_option_for_condition_and_token",
    "resolve_binary_option_for_wallet_warmup",
]
