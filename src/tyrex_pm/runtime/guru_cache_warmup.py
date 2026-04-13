"""
**Startup cache warmup:** preload ``Cache`` from recent guru Data API ``/activity`` rows
and from the follower wallet's Data API ``/positions`` rows.

**Data API ``/positions`` row shape** is aligned with **package-source**
``nautilus_trader.adapters.polymarket.execution.PolymarketExecutionClient._fetch_quantities_from_gamma_api``,
which maps ``conditionId``, ``asset`` (outcome / CLOB token id), and ``size``.

**Scope:** current holdings returned by ``/positions`` at compose time only — not full trade
history. Nautilus may still log ``instrument … not found`` for **historical** trades on tokens
that were never warmed into ``Cache``; that noise is **orthogonal** to “did we hydrate today’s
open positions?”.

**Not** a second source of trading truth — only opportunistic resolution into Nautilus ``Cache``.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass
from typing import Any

import httpx

from tyrex_pm.config.loaders import RuntimeSettings
from tyrex_pm.data.data_api_client import PolymarketDataApiClient
from tyrex_pm.runtime.clob_factory import build_clob_client_from_env
from tyrex_pm.runtime.guru_instrument_dynamic import GuruInstrumentDynamicController

_LOG = logging.getLogger(__name__)

# Summary ``warmup_outcome=`` on ``wallet_position_warmup_done`` (machine-parseable).
WARMUP_OUTCOME_EMPTY_POSITIONS_API = "empty_positions_api"
WARMUP_OUTCOME_NO_ELIGIBLE_ROWS = "no_eligible_rows"
WARMUP_OUTCOME_SUCCESS = "success"
WARMUP_OUTCOME_PARTIAL = "partial"
WARMUP_OUTCOME_FAILURE_ALL = "failure_all_resolvable"
WARMUP_OUTCOME_UNKNOWN = "unknown"

# Optional operator hint keyed by :class:`~tyrex_pm.runtime.guru_instrument_dynamic.GuruInstrumentResolveError.detail`.
_RESOLVE_DETAIL_HINTS: dict[str, str] = {
    "gamma_empty": "Gamma /markets?clob_token_ids returned no row for this token (and no usable conditionId fallback).",
    "gamma_bad_shape": "Gamma response was not a list of market objects.",
    "gamma_no_condition": "Gamma market row missing conditionId.",
    "clob_error_string": "py-clob get_market returned an error string (see msg=).",
    "clob_bad_type": "py-clob get_market returned an unexpected non-dict payload.",
    "clob_token_missing": "CLOB market tokens[] has no matching token_id (wrong market or schema drift).",
    "parse_failed": "parse_polymarket_instrument rejected this market/outcome (unsupported or invalid for BinaryOption path).",
    "http_error": "HTTP failure calling Gamma or CLOB during resolve.",
    "unexpected_error": "Non-HTTP exception during resolve (see Tyrex traceback).",
    "resolve_unspecified": "Resolution failed without a classified detail code.",
    "activation_cap": "Should not occur for wallet warmup (force_add); report as bug.",
    "cache_inconsistent": "Instrument added but missing from Cache — report as bug.",
}


@dataclass(frozen=True, slots=True)
class WalletPositionRowExtract:
    """
    Result of normalizing one ``GET /positions`` row for warmup.

    ``skip_reason`` is set when this row must not be passed to resolution (flat, bad shape, etc.).
    ``failure_reason`` is set when the row is non-flat and should have resolved a token but cannot
    (e.g. missing ``asset``) — caller should log once per row.
    """

    size: float
    token_id: str | None
    condition_id: str | None
    skip_reason: str | None = None
    failure_reason: str | None = None


def extract_wallet_position_row_fields(row: Any) -> WalletPositionRowExtract:
    """
    Parse one Data API ``/positions`` element into token + condition + size.

    **Canonical fields** (Nautilus Polymarket adapter):
    - Outcome / CLOB token id: ``asset``
    - Market key: ``conditionId``
    - Position size: ``size``

    **Optional aliases** (explicit, same semantics; no alternate ``InstrumentId`` rules):
    - Token: ``tokenId``, ``clobTokenId`` if ``asset`` is absent
    - Condition: ``condition_id`` if ``conditionId`` is absent

    A **missing** ``size`` key (distinct from ``size: 0``) is treated as **malformed** so
    API drift does not silently look like a flat position.
    """
    if not isinstance(row, dict):
        return WalletPositionRowExtract(
            size=0.0,
            token_id=None,
            condition_id=None,
            skip_reason="invalid_row_type",
        )

    if "size" not in row:
        return WalletPositionRowExtract(
            size=0.0,
            token_id=None,
            condition_id=None,
            skip_reason="missing_size_key",
        )

    size_raw = row.get("size")  # key exists (may be None)
    if size_raw is None:
        return WalletPositionRowExtract(
            size=0.0,
            token_id=None,
            condition_id=None,
            skip_reason="null_size",
        )
    try:
        sz = float(size_raw)
    except (TypeError, ValueError):
        return WalletPositionRowExtract(
            size=0.0,
            token_id=None,
            condition_id=None,
            skip_reason="invalid_size",
        )

    if sz <= 1e-12:
        return WalletPositionRowExtract(
            size=sz,
            token_id=None,
            condition_id=None,
            skip_reason="flat_size",
        )

    token: str | None = None
    for key in ("asset", "tokenId", "clobTokenId"):
        v = row.get(key)
        if v is not None:
            s = str(v).strip()
            if s:
                token = s
                break

    cond_raw = row.get("conditionId")
    if cond_raw is None:
        cond_raw = row.get("condition_id")
    condition_id = str(cond_raw).strip() if cond_raw is not None else None
    if condition_id == "":
        condition_id = None

    if token is None:
        return WalletPositionRowExtract(
            size=sz,
            token_id=None,
            condition_id=condition_id,
            failure_reason="missing_outcome_token_field",
        )

    return WalletPositionRowExtract(
        size=sz,
        token_id=token,
        condition_id=condition_id,
    )


def warm_polymarket_cache_from_guru_activity(
    controller: GuruInstrumentDynamicController,
    *,
    guru_wallet_address: str,
    runtime: RuntimeSettings,
) -> int:
    """
    Resolve up to ``runtime.polymarket_startup_token_warmup_max`` distinct outcome tokens from
    the latest guru TRADE activity. Returns count successfully present in ``Cache`` afterward.
    """
    cap = int(runtime.polymarket_startup_token_warmup_max)
    if cap <= 0:
        return 0

    client = PolymarketDataApiClient(runtime.data_api_base_url, log_backoff=None)
    limit = max(1, min(500, int(runtime.guru_activity_limit)))

    try:
        rows = client.get_user_trade_activity(
            user=guru_wallet_address,
            limit=limit,
            offset=0,
            sort_direction="DESC",
            sort_by="TIMESTAMP",
        )
    except Exception:
        _LOG.warning(
            "event=guru_cache_warmup_failed component=guru_cache_warmup detail=data_api",
            exc_info=True,
        )
        return 0

    seen: set[str] = set()
    warmed = 0
    for row in rows:
        if warmed >= cap:
            break
        asset = row.get("asset")
        if asset is None:
            continue
        tid = str(asset).strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        inst, fail = controller.resolve_and_activate(tid)
        if inst is not None:
            warmed += 1
        elif fail:
            hint = _RESOLVE_DETAIL_HINTS.get(fail, "")
            _LOG.info(
                "event=guru_cache_warmup_resolve_skip component=guru_cache_warmup "
                "token_id=%s detail=%s hint=%s",
                tid[:24],
                fail,
                hint[:200] if hint else "",
            )

    _LOG.info(
        "event=guru_cache_warmup_done component=guru_cache_warmup distinct=%s warmed=%s cap=%s",
        len(seen),
        warmed,
        cap,
    )
    return warmed


def _follower_positions_api_user(*, runtime: RuntimeSettings) -> str:
    """
    Address passed as ``user`` to Data API ``GET /positions``.

    **Must match** Nautilus Polymarket ``PolymarketExecutionClient`` ``user_address``:
    ``POLYMARKET_FUNDER`` when set (proxy / signature types 1–2 — **funder holds
    positions**), otherwise the signer address from ``ClobClient.get_address()``.
    """
    funder = os.environ.get("POLYMARKET_FUNDER", "").strip()
    if funder:
        return funder
    clob = build_clob_client_from_env(runtime)
    addr = clob.get_address()
    if not addr:
        raise RuntimeError("Cannot resolve wallet address for positions warmup")
    return str(addr)


def fetch_wallet_position_rows(
    *,
    user_address: str,
    data_api_base_url: str,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """
    Paginated ``GET /positions`` (Polymarket Data API), same parameters as Nautilus adapter.

    Each element is expected to be a JSON object with at least ``size``; non-flat rows
    should carry ``asset`` (or an accepted alias) per :func:`extract_wallet_position_row_fields`.
    """
    base = data_api_base_url.rstrip("/")
    url = f"{base}/positions"
    results: list[dict[str, Any]] = []
    limit = 100
    offset = 0
    while True:
        params = {
            "user": user_address,
            "limit": str(limit),
            "offset": str(offset),
            "sizeThreshold": "0",
            "sortBy": "TOKENS",
            "sortDirection": "DESC",
        }
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, params=params)
        if resp.status_code >= 400:
            resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        if not isinstance(data, list):
            _LOG.warning(
                "event=wallet_position_fetch component=guru_cache_warmup "
                "detail=positions_response_not_list type=%s",
                type(data).__name__,
            )
            break
        results.extend(data)
        if len(data) < limit:
            break
        offset += limit
        if offset > 10_000:
            _LOG.warning("event=wallet_position_fetch component=guru_cache_warmup detail=offset_cap")
            break
    return results


def warm_polymarket_cache_from_wallet_positions(
    controller: GuruInstrumentDynamicController,
    *,
    runtime: RuntimeSettings,
) -> int:
    """
    For each non-zero ``/positions`` row, resolve the outcome token into ``Cache``.

    Uses :meth:`GuruInstrumentDynamicController.resolve_and_activate_wallet_position` so
    **held** markets do not consume the guru ``polymarket_dynamic_max_activations`` budget.
    Passes ``conditionId`` from the row when present so CLOB+parse can retry if Gamma fails.
    """
    cap = int(runtime.polymarket_wallet_position_warmup_max)
    if cap <= 0:
        return 0

    try:
        user = _follower_positions_api_user(runtime=runtime)
    except Exception:
        _LOG.warning(
            "event=wallet_position_warmup_skip component=guru_cache_warmup "
            "reason=no_follower_user_address",
            exc_info=True,
        )
        return 0

    try:
        rows = fetch_wallet_position_rows(
            user_address=user,
            data_api_base_url=runtime.data_api_base_url,
            timeout=max(5.0, float(runtime.polymarket_gamma_http_timeout_seconds)),
        )
    except Exception:
        _LOG.warning(
            "event=wallet_position_warmup_failed component=guru_cache_warmup detail=data_api",
            exc_info=True,
        )
        return 0

    if len(rows) == 0:
        _LOG.info(
            "event=wallet_position_warmup_data_api_empty component=guru_cache_warmup "
            "positions_user=%s data_api_base_url=%s "
            "warmup_outcome=%s "
            "hint=no_open_positions_for_user_or_verify_POLYMARKET_FUNDER_vs_signer "
            "note=historical_trade_reconciliation_may_still_emit_instrument_not_found",
            user.lower(),
            runtime.data_api_base_url.rstrip("/"),
            WARMUP_OUTCOME_EMPTY_POSITIONS_API,
        )

    seen: set[str] = set()
    warmed = 0
    skipped_flat = 0
    skipped_malformed = 0
    resolution_failures = 0
    failure_detail_counts: Counter[str] = Counter()
    truncated_by_cap = False
    for idx, row in enumerate(rows, start=1):
        if warmed >= cap:
            truncated_by_cap = idx <= len(rows)
            break
        ex = extract_wallet_position_row_fields(row)
        if ex.skip_reason == "flat_size":
            skipped_flat += 1
            continue
        if ex.skip_reason:
            skipped_malformed += 1
            _LOG.warning(
                "event=wallet_position_warmup_row_skip component=guru_cache_warmup "
                "reason=%s row_index=%s/%s",
                ex.skip_reason,
                idx,
                len(rows),
            )
            continue
        if ex.failure_reason:
            skipped_malformed += 1
            _LOG.warning(
                "event=wallet_position_warmup_row_skip component=guru_cache_warmup "
                "reason=%s row_index=%s/%s size=%.6g condition_id_prefix=%s",
                ex.failure_reason,
                idx,
                len(rows),
                ex.size,
                (ex.condition_id or "")[:16],
            )
            continue

        tid = ex.token_id
        assert tid is not None
        if tid in seen:
            continue
        seen.add(tid)

        outcome = controller.resolve_and_activate_wallet_position(
            tid,
            row_condition_id=ex.condition_id,
        )
        inst = outcome.instrument
        fail_tag = outcome.detail
        fail_msg = outcome.message
        if inst is not None:
            warmed += 1
        else:
            resolution_failures += 1
            failure_detail_counts[fail_tag] += 1
            hint = _RESOLVE_DETAIL_HINTS.get(fail_tag, "")
            msg_s = (fail_msg or "")[:200].replace("\n", " ")
            _LOG.warning(
                "event=wallet_position_warmup_row_failed component=guru_cache_warmup "
                "detail=%s token_id=%s condition_id_prefix=%s size=%.6g row_index=%s/%s "
                "msg=%s hint=%s",
                fail_tag,
                tid[:24],
                (ex.condition_id or "")[:16],
                ex.size,
                idx,
                len(rows),
                msg_s,
                hint[:200] if hint else "",
            )

    attempted = len(seen)
    if len(rows) == 0:
        warmup_outcome = WARMUP_OUTCOME_EMPTY_POSITIONS_API
    elif attempted == 0:
        warmup_outcome = WARMUP_OUTCOME_NO_ELIGIBLE_ROWS
    elif resolution_failures == 0 and warmed > 0:
        warmup_outcome = WARMUP_OUTCOME_SUCCESS
    elif resolution_failures > 0 and warmed > 0:
        warmup_outcome = WARMUP_OUTCOME_PARTIAL
    elif resolution_failures > 0 and warmed == 0:
        warmup_outcome = WARMUP_OUTCOME_FAILURE_ALL
    else:
        warmup_outcome = WARMUP_OUTCOME_UNKNOWN

    fdc = ",".join(f"{k}:{v}" for k, v in sorted(failure_detail_counts.items())) or "none"

    _LOG.info(
        "event=wallet_position_warmup_done component=guru_cache_warmup "
        "warmup_outcome=%s distinct=%s warmed=%s cap=%s rows=%s flat_skipped=%s "
        "malformed_skipped=%s resolution_failures=%s failure_details=%s "
        "truncated_by_cap=%s positions_user=%s",
        warmup_outcome,
        len(seen),
        warmed,
        cap,
        len(rows),
        skipped_flat,
        skipped_malformed,
        resolution_failures,
        fdc,
        truncated_by_cap,
        user.lower(),
    )
    return warmed


__all__ = [
    "WARMUP_OUTCOME_EMPTY_POSITIONS_API",
    "WARMUP_OUTCOME_FAILURE_ALL",
    "WARMUP_OUTCOME_NO_ELIGIBLE_ROWS",
    "WARMUP_OUTCOME_PARTIAL",
    "WARMUP_OUTCOME_SUCCESS",
    "WARMUP_OUTCOME_UNKNOWN",
    "WalletPositionRowExtract",
    "extract_wallet_position_row_fields",
    "fetch_wallet_position_rows",
    "warm_polymarket_cache_from_guru_activity",
    "warm_polymarket_cache_from_wallet_positions",
]
