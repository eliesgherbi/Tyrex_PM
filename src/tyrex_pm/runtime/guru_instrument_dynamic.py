"""
Dynamic guru ``token_id`` → Nautilus ``BinaryOption`` + ``Cache`` activation (**Step 5**).

**Resolution (public HTTP, Tyrex-owned orchestration):**
  #. Gamma Get Markets with ``clob_token_ids`` → ``conditionId`` (**Docs-confirmed** Gamma API;
     Tyrex uses the documented filter shape from **Package-source-confirmed**
     ``nautilus_trader.adapters.polymarket.common.gamma_markets.build_markets_query``).
  #. py-clob ``get_market(condition_id)`` for CLOB-shaped market dict (**Repo-confirmed**
     ``PolymarketInstrumentProvider`` uses the same call).
  #. ``parse_polymarket_instrument`` (**Package-source-confirmed** ``common.parsing``).

**Activation:**
  #. ``Cache.add_instrument`` + ``add_currency`` when needed — **public** ``Cache`` API
     (**Package-source-confirmed**; same primitive ``PolymarketDataClient`` uses for instruments).

This does **not** patch Nautilus clients. Exec client **``_maintain_active_market``** still runs on
submit (**Package-source-confirmed**). Tyrex never owns a second instrument truth store beyond
what enters ``Cache``.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

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

    return parse_polymarket_instrument(
        market_info=raw,
        token_id=str(token_id),
        outcome=outcome,
        ts_init=time.time_ns(),
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

        Returns ``(instrument, "")`` on success, or ``(None, failure_tag)`` where ``failure_tag`` is
        ``"resolve_failed"`` or ``"activation_cap"`` for observability.
        """
        tid = str(token_id)
        for cached in self._cache.instruments(venue=Venue(POLYMARKET)):
            if str(get_polymarket_token_id(cached.id)) == tid:
                return cached, ""

        try:
            inst = resolve_binary_option_for_clob_token(
                token_id,
                self._clob,
                gamma_base_url=self._runtime.polymarket_gamma_base_url,
                http_timeout=float(self._runtime.polymarket_gamma_http_timeout_seconds),
            )
        except GuruInstrumentResolveError:
            return None, "resolve_failed"
        except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError):
            return None, "resolve_failed"

        ok, _reason = self._activator.try_add_instrument(inst)
        if not ok:
            return None, "activation_cap"
        cached = self._cache.instrument(inst.id)
        if cached is None:
            return None, "resolve_failed"
        return cached, ""
