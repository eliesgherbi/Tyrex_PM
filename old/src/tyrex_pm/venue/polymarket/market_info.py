"""Per-market metadata adapter (Phase 5).

Why this module exists
----------------------
Before Phase 5 the bot hard-coded the two venue parameters that change per market:

* ``min_order_size`` — Polymarket's hard floor for ``size``. Defaulted to ``5`` shares
  via ``risk.venue_min_size.default_min_size``. The default is correct for binary
  markets but wrong for some markets where the venue truth is different (live
  evidence: ``Size (1) lower than the minimum: 5`` rejection on a market whose
  ``mos`` is actually ``5`` — same value, but the bot had no way to *know* that
  before submit, so a market with ``mos=20`` would have surprised us).
* ``tick_size`` — minimum price increment. The V2 SDK auto-resolves it on every
  ``create_and_post_order`` (one extra REST round-trip per submit). Worse, when
  a strategy emits an unaligned ``limit_price`` (e.g. ``0.5523`` on a tick-0.01
  market), the venue rejects with a precision error.

This module owns a single in-process cache of resolved per-market truth so that:

1. The risk engine's :mod:`tyrex_pm.risk.venue_min_size` gate uses the *venue's*
   ``mos`` instead of a YAML default.
2. The OMS/order-builder boundary can quantize ``limit_price`` to the venue's
   ``mts`` before submit, with evidence in the ``oms_submit`` fact.
3. ``live-attest`` can write a ``market_info`` evidence fact at startup so an
   operator inspecting facts.jsonl sees exactly which tick/min-size were in
   effect for the attested order.

V2 endpoints used
-----------------
``/markets-by-token/<token_id>`` — token_id → ``condition_id`` (one round-trip,
no SDK helper exists; we hit it via ``httpx`` directly).

``/clob-markets/<condition_id>`` — full per-market metadata: ``mts`` (tick
size), ``mos`` (minimum order size), ``t`` (token list with outcome labels).
This is the single source of truth.

``ClobClient.get_neg_risk(token_id) -> bool`` — neg-risk flag (the SDK already
caches this internally).

``ClobClient.get_fee_rate_bps(token_id) -> int`` — base fee in bps (also SDK
cached).

Import isolation
~~~~~~~~~~~~~~~~
This module lives under ``src/tyrex_pm/venue/polymarket/`` and is the *only*
non-bridge consumer of the V2 SDK in the venue layer. The risk engine and
runtime layers consume the resolved :class:`MarketInfo` dataclass via
``RiskContext.market_info``; they never import the V2 SDK directly.

Cache semantics
~~~~~~~~~~~~~~~
* TTL: ``ttl_s`` (default 300s). Tick size and min order size very rarely change
  during a market's life, so 5 minutes is a generous-but-safe refresh interval.
* Fail-closed: if any lookup fails, ``get`` raises. Callers that need to fall
  back to YAML defaults (e.g. shadow mode without a live client) must construct
  a coordinator with ``market_info_cache=None`` instead of catching here.
* Concurrency: a single asyncio lock per cache instance serializes refreshes
  for the *same* token; different tokens proceed in parallel. The hot path
  (cache hit) takes the lock only briefly.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from tyrex_pm.core.ids import TokenId

log = logging.getLogger(__name__)


_DEFAULT_TTL_S = 300.0
_DEFAULT_HTTP_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class MarketInfo:
    """Resolved per-market venue truth for a single ``token_id``.

    All numeric fields are :class:`Decimal` so the risk engine and order
    builder can compose them with the rest of the Decimal arithmetic without
    introducing float-rounding bugs at quantize boundaries.
    """

    token_id: TokenId
    condition_id: str
    tick_size: Decimal
    min_order_size: Decimal
    neg_risk: bool
    fee_rate_bps: int
    #: ``{token_id: outcome_label}`` for both outcomes of the market (e.g.
    #: ``{"...154": "Yes", "...033": "No"}``). Used by ``live-attest`` evidence
    #: and future outcome-validation work.
    outcomes: dict[str, str]
    #: When this entry was fetched from the venue. Used to enforce ``ttl_s``.
    fetched_ts: datetime
    #: Raw response from ``/clob-markets/<condition_id>`` for forensic facts.
    raw: dict[str, Any] = field(default_factory=dict)

    def quantize_price(self, price: Decimal) -> Decimal:
        """Round ``price`` *down* to the nearest multiple of ``tick_size``.

        Rounding *down* is intentional: a BUY at quantized-down price is
        cheaper (safer); a SELL at quantized-down price is just as legal as
        the original since the venue accepts any tick-aligned price. We do
        not round-up because that could push a BUY past the operator's
        intended limit. Callers that need rounding-to-nearest can always
        do so before passing the price in.
        """
        if self.tick_size <= 0:
            return price
        # Decimal // gives floor-division for positive values.
        ticks = (price / self.tick_size).to_integral_value(rounding="ROUND_DOWN")
        return (ticks * self.tick_size).normalize()


class MarketInfoFetchError(RuntimeError):
    """Raised when the venue refuses to return market info for a token.

    Distinguished from ``httpx.HTTPError`` so callers can match on
    "venue truth missing" specifically.
    """


class MarketInfoCache:
    """Async in-process cache of :class:`MarketInfo` keyed by ``token_id``.

    A single instance is owned by :class:`tyrex_pm.runtime.coordinator.RuntimeCoordinator`
    and shared across the live-mode risk + execution path. Shadow mode passes
    ``None`` and the consumers fall back to YAML defaults.

    The cache is intentionally non-persistent: a process restart re-resolves
    each market on first use. Tick/min-size are not secrets and the cost is
    one round-trip per market per restart.
    """

    def __init__(
        self,
        client: Any,
        *,
        host: str,
        ttl_s: float = _DEFAULT_TTL_S,
        http_timeout_s: float = _DEFAULT_HTTP_TIMEOUT_S,
    ) -> None:
        self._client = client
        self._host = host.rstrip("/")
        self._ttl_s = float(ttl_s)
        self._timeout_s = float(http_timeout_s)
        self._cache: dict[str, MarketInfo] = {}
        self._lock = asyncio.Lock()

    @property
    def ttl_s(self) -> float:
        return self._ttl_s

    def snapshot(self) -> dict[TokenId, MarketInfo]:
        """Return a shallow copy of the currently-resolved entries.

        Used by :meth:`RuntimeCoordinator.build_risk_context` to inject the
        per-token venue truth into ``RiskContext.market_info`` *without*
        forcing a refresh on the hot path. New tokens must be resolved by
        calling :meth:`get` before risk evaluation; this method only mirrors
        what is already known.
        """
        return {TokenId(tid): info for tid, info in self._cache.items()}

    async def get(self, token_id: TokenId | str) -> MarketInfo:
        """Return :class:`MarketInfo` for ``token_id``, refreshing if stale.

        Fail-closed: any HTTP or parse error propagates as
        :class:`MarketInfoFetchError`. Callers decide whether to retry or
        skip the intent.
        """
        tid = str(token_id)
        cached = self._cache.get(tid)
        now = datetime.now(timezone.utc)
        if cached is not None and (now - cached.fetched_ts).total_seconds() < self._ttl_s:
            return cached

        async with self._lock:
            cached = self._cache.get(tid)
            now = datetime.now(timezone.utc)
            if cached is not None and (now - cached.fetched_ts).total_seconds() < self._ttl_s:
                return cached

            info = await self._fetch(tid)
            self._cache[tid] = info
            return info

    async def _fetch(self, token_id: str) -> MarketInfo:
        condition_id = await self._resolve_condition_id(token_id)
        market = await self._fetch_clob_market(condition_id)

        try:
            tick_size = Decimal(str(market["mts"]))
            min_order_size = Decimal(str(market["mos"]))
        except (KeyError, TypeError) as e:
            raise MarketInfoFetchError(
                f"clob-markets/{condition_id} response missing mts/mos: {e!r}"
            ) from e

        outcomes: dict[str, str] = {}
        for tok in market.get("t") or ():
            if not isinstance(tok, dict):
                continue
            t_id = tok.get("t")
            label = tok.get("o")
            if t_id and label:
                outcomes[str(t_id)] = str(label)

        # SDK helpers (sync; wrap in to_thread). The SDK has its own per-token
        # in-memory cache for both, so repeated calls are cheap.
        try:
            neg_risk = bool(
                await asyncio.to_thread(self._client.get_neg_risk, token_id)
            )
        except Exception as e:  # noqa: BLE001 — venue truth is fail-closed
            raise MarketInfoFetchError(
                f"get_neg_risk({token_id}) failed: {e!r}"
            ) from e
        try:
            fee_rate_bps = int(
                await asyncio.to_thread(self._client.get_fee_rate_bps, token_id)
            )
        except Exception as e:  # noqa: BLE001
            raise MarketInfoFetchError(
                f"get_fee_rate_bps({token_id}) failed: {e!r}"
            ) from e

        return MarketInfo(
            token_id=TokenId(token_id),
            condition_id=str(condition_id),
            tick_size=tick_size,
            min_order_size=min_order_size,
            neg_risk=neg_risk,
            fee_rate_bps=fee_rate_bps,
            outcomes=outcomes,
            fetched_ts=datetime.now(timezone.utc),
            raw=market,
        )

    async def _resolve_condition_id(self, token_id: str) -> str:
        url = f"{self._host}/markets-by-token/{token_id}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as http:
                resp = await http.get(url)
        except httpx.HTTPError as e:
            raise MarketInfoFetchError(
                f"markets-by-token({token_id}) HTTP error: {e!r}"
            ) from e
        if resp.status_code != 200:
            raise MarketInfoFetchError(
                f"markets-by-token({token_id}) -> HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise MarketInfoFetchError(
                f"markets-by-token({token_id}) returned non-JSON: {e!r}"
            ) from e
        cond = data.get("condition_id") if isinstance(data, dict) else None
        if not cond:
            raise MarketInfoFetchError(
                f"markets-by-token({token_id}) missing condition_id; got {data!r}"
            )
        return str(cond)

    async def _fetch_clob_market(self, condition_id: str) -> dict[str, Any]:
        url = f"{self._host}/clob-markets/{condition_id}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as http:
                resp = await http.get(url)
        except httpx.HTTPError as e:
            raise MarketInfoFetchError(
                f"clob-markets({condition_id}) HTTP error: {e!r}"
            ) from e
        if resp.status_code != 200:
            raise MarketInfoFetchError(
                f"clob-markets({condition_id}) -> HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise MarketInfoFetchError(
                f"clob-markets({condition_id}) returned non-JSON: {e!r}"
            ) from e
        if not isinstance(data, dict):
            raise MarketInfoFetchError(
                f"clob-markets({condition_id}) returned non-object: {type(data).__name__}"
            )
        return data
