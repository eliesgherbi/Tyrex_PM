"""YAML loaders with validation (secrets stay in ``.env``)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_token_id
from nautilus_trader.model.identifiers import InstrumentId


def _root(data: Any, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be a mapping")
    return data


@dataclass(frozen=True, slots=True)
class TokenFilterSettings:
    """Strategy YAML ``token_filter`` block."""

    enabled: bool
    allowlisted_token_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StrategySettings:
    guru_wallet_address: str
    token_filter: TokenFilterSettings
    copy_scale: float
    strategy_dedup_state_path: str | None = None
    #: C2 — conviction-weighted sizing (default off = pre-C2 behavior).
    conviction_sizing_enabled: bool = False
    conviction_sizing_cap: float = 2.0
    conviction_sizing_lookback_trades: int = 20
    #: C2 — skip when ``price_ref * qty`` below this USD floor (0 = disabled).
    min_follow_notional_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class RiskSettings:
    """
    Pre-trade risk parameters.

    **Phase B contract (B0+):** Framework-truth-dependent gates —
    finite ``max_portfolio_notional_usd_open``, non-null
    ``max_concurrent_guru_resting_orders`` — require **live** runtime with
    ``polymarket_nautilus_live`` and ``polymarket_framework_submit`` (see
    :func:`validate_phase_b_runtime_contract`). Semantics for measured portfolio
    exposure are normative in ``Docs/Implementation/Phase_B_planing.md`` §§4–7;
    **B0** validation; **B1** aggregation; **B2** portfolio-wide cap in ``ConfiguredRiskPolicy``;
    **B3–B4** concurrency / reserve (separate).
    """

    max_order_quantity: float
    max_notional_usd_per_order: float
    max_token_notional_usd_open: float
    kill_switch: bool
    fail_on_missing_price_for_notional: bool
    #: When true, pre-trade risk requires fresh account + allowance snapshots (live ops).
    capital_gate_enabled: bool = False
    #: Maximum age of the cached **account** snapshot before refresh; fail-closed if refresh fails.
    max_account_snapshot_age_seconds: float = 30.0
    #: Maximum age of the cached **allowance** snapshot before refresh.
    max_allowance_snapshot_age_seconds: float = 120.0
    #: Optional minimum collateral balance (USDC, same units as py-clob ``balance`` strings).
    min_collateral_balance_usd: float | None = None
    #: Optional minimum allowance (USDC) from py-clob ``allowance`` field.
    min_allowance_usd: float | None = None
    #: If true and ``max_token_notional_usd_open`` is finite, deny when position exposure cannot
    #: be resolved (instrument not in cache / ``net_exposure`` unavailable).
    fail_on_unresolved_position_for_token_cap: bool = False
    #: **Phase B:** Portfolio-wide open notional cap vs ``E_portfolio + n`` (B2). ``inf``/omitted =
    #: disabled. **Framework-only:** requires Nautilus live + framework submit (validated at compose).
    #: Scope: Polymarket venue, this node’s ``Cache``/``Portfolio`` only (see Phase B plan §4.1).
    max_portfolio_notional_usd_open: float = float("inf")
    #: **Phase B:** When B2 is implemented, default **true** = fail-closed if a portfolio mark cannot
    #: be resolved for an instrument in scope (Phase B plan §4.6). Inert until B2 unless portfolio cap finite.
    fail_on_unresolved_portfolio_exposure: bool = True
    #: **Phase B:** Max concurrent **guru-origin** resting orders (B3). ``None`` = disabled.
    #: **Framework-only** — legacy py-clob submit has no ``Cache``-visible guru order truth.
    max_concurrent_guru_resting_orders: int | None = None
    #: **Phase B B4:** USDC collateral floor held back from new **BUY** risk after Phase A mins.
    #: **Requires** ``capital_gate_enabled`` (same py-clob ``balance`` snapshot as Phase A).
    #: **Invalid** with shadow runtime (validated at compose). Denies when
    #: ``balance < collateral_reserve_usd + n`` (``RISK_INSUFFICIENT_FREE_COLLATERAL_AFTER_RESERVE``).
    collateral_reserve_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    trader_id: str
    execution_mode: str
    guru_poll_interval_seconds: float
    data_api_base_url: str
    guru_dedup_state_path: str
    guru_state_path: str
    guru_activity_limit: int
    guru_startup_backfill_seconds: float
    guru_max_activity_pages_per_poll: int
    logging_level: str
    clob_host: str
    chain_id: int
    #: When true **and** ``execution_mode == "live"``, register Polymarket Nautilus data+exec
    #: factories (Path A). Empty ``polymarket_instrument_ids`` is allowed only with
    #: ``polymarket_framework_submit`` (guru dynamic universe — **Repo-confirmed** Tyrex).
    polymarket_nautilus_live: bool
    #: Full ``InstrumentId`` strings (``condition_id-token_id.POLYMARKET``); shared
    #: ``load_ids`` for data + exec clients.
    polymarket_instrument_ids: tuple[str, ...]
    #: **Step 4:** With live + ``polymarket_nautilus_live``, guru submit uses Nautilus
    #: ``submit_order`` (not py-clob).
    polymarket_framework_submit: bool
    #: Outcome ``token_id`` → full ``InstrumentId`` string (from ``polymarket_instrument_ids``).
    polymarket_token_to_instrument: tuple[tuple[str, str], ...]
    #: **Step 5:** Gamma+CLOB resolve for unknown guru ``token_id``; activate into ``Cache``
    #: (requires framework submit).
    polymarket_dynamic_instruments: bool
    #: **Step 5:** Max **new** dynamic cache inserts per process (0 = no new adds).
    polymarket_dynamic_max_activations: int
    #: **Step 5:** Gamma HTTP API base (Get Markets with ``clob_token_ids``).
    polymarket_gamma_base_url: str
    #: **Step 5:** Timeout for Gamma HTTP calls (seconds).
    polymarket_gamma_http_timeout_seconds: float
    #: **Step 5:** Max guru outcome tokens to pre-resolve at node build from Data API ``/activity``
    #: (0 = skip self-bootstrap). Only used when ``polymarket_instrument_ids`` is empty.
    polymarket_startup_token_warmup_max: int
    #: **C1:** ``poll_only`` | ``rtds_shadow`` | ``rtds_primary``
    guru_ingest_mode: str = "poll_only"
    #: **C1:** optional rollout label for logs (informational).
    guru_ingest_phase: str = "0"
    guru_rtds_url: str = "wss://ws-live-data.polymarket.com"
    guru_rtds_liveness_timeout_seconds: float = 120.0
    guru_rtds_reconnect_retry_initial_seconds: float = 1.0
    guru_rtds_reconnect_retry_max_seconds: float = 60.0
    guru_rtds_ping_interval_seconds: float = 5.0
    guru_poll_fallback_enabled: bool = True
    guru_poll_fallback_interval_seconds: float | None = None
    guru_gap_fill_enabled: bool = True
    guru_gap_fill_lookback_seconds: float = 60.0
    guru_proxy_wallet_validation_required: bool = False
    guru_stream_queue_drain_interval_ms: int = 50
    #: ---- C3 execution quality (framework ``NautilusGuruExecutionPort`` only; default off) ----
    execution_venue_normalize_enabled: bool = False
    execution_entry_guard_enabled: bool = False
    #: Max ticks market may move against follower vs guru reference (0 = treat guard as off).
    execution_max_entry_slippage_ticks: int = 0
    execution_book_depth_clip_enabled: bool = False
    execution_book_depth_utilization_cap: float = 1.0
    execution_book_rest_snapshot_enabled: bool = False
    #: If true and guard/clip need L2 but no snapshot: skip submit; if false: skip feature only.
    execution_book_strict: bool = False
    execution_limit_timeout_enabled: bool = False
    execution_limit_timeout_seconds: float = 30.0
    #: Persist structured reporting facts under ``var/reporting/runs/<run_id>/`` when true.
    reporting_enabled: bool = False
    reporting_base_dir: str = "var/reporting/runs"
    reporting_sink_max_queue: int = 50_000
    reporting_sink_batch_size: int = 128


def _polymarket_token_instrument_map(
    poly_ids: tuple[str, ...],
    *,
    path: Path,
) -> tuple[tuple[str, str], ...]:
    """
    **Package-source-confirmed:** ``get_polymarket_token_id`` extracts CLOB token
    from ``InstrumentId``.
    """
    m: dict[str, str] = {}
    for instr_s in poly_ids:
        try:
            iid = InstrumentId.from_str(instr_s)
            tid = get_polymarket_token_id(iid)
        except ValueError as exc:
            raise ValueError(
                f"{path}: invalid polymarket_instrument_ids entry {instr_s!r}: {exc}",
            ) from exc
        if tid in m and m[tid] != instr_s:
            raise ValueError(
                f"{path}: duplicate outcome token_id {tid!r} in polymarket_instrument_ids "
                f"({m[tid]!r} vs {instr_s!r})",
            )
        m[tid] = instr_s
    return tuple(sorted(m.items()))


def _normalize_token_list(raw_list: list[Any], *, path: Path, ctx: str) -> tuple[str, ...]:
    norm = tuple(str(x).strip() for x in raw_list if str(x).strip())
    if len(norm) != len(set(norm)):
        raise ValueError(f"{path}: duplicate token ids in {ctx}")
    return norm


def load_strategy_settings(path: str | Path) -> StrategySettings:
    p = Path(path)
    raw = _root(yaml.safe_load(p.read_text(encoding="utf-8")), p)
    if "guru_wallet_address" not in raw:
        raise ValueError(f"{p}: missing required key: guru_wallet_address")
    gw = str(raw["guru_wallet_address"]).strip()
    if not gw.startswith("0x") or len(gw) != 42:
        raise ValueError(f"{p}: guru_wallet_address must be 0x + 40 hex chars")

    tf_raw = raw.get("token_filter")
    if not isinstance(tf_raw, dict):
        raise ValueError(f"{p}: token_filter must be a mapping")
    if "enabled" not in tf_raw:
        raise ValueError(f"{p}: token_filter.enabled is required")

    enabled = bool(tf_raw["enabled"])
    tokens_field = tf_raw.get("allowlisted_token_ids")
    if tokens_field is None:
        tokens_field = []
    if not isinstance(tokens_field, list):
        raise ValueError(f"{p}: token_filter.allowlisted_token_ids must be a list")

    norm = _normalize_token_list(
        tokens_field,
        path=p,
        ctx="token_filter.allowlisted_token_ids",
    )
    if enabled and not norm:
        raise ValueError(
            f"{p}: when token_filter.enabled is true, "
            "token_filter.allowlisted_token_ids must be non-empty"
        )

    token_filter = TokenFilterSettings(enabled=enabled, allowlisted_token_ids=norm)

    scale = float(raw.get("copy_scale", 1.0))
    if scale < 0:
        raise ValueError(f"{p}: copy_scale must be >= 0")

    dedup = raw.get("strategy_dedup_state_path")
    dedup_s = str(dedup).strip() if dedup else None

    conv_en = bool(raw.get("conviction_sizing_enabled", False))
    conv_cap = float(raw.get("conviction_sizing_cap", 2.0))
    lookback = int(raw.get("conviction_sizing_lookback_trades", 20))
    min_follow = float(raw.get("min_follow_notional_usd", 0.0))

    if min_follow < 0:
        raise ValueError(f"{p}: min_follow_notional_usd must be >= 0")
    if conv_en:
        if lookback < 1:
            raise ValueError(f"{p}: conviction_sizing_lookback_trades must be >= 1 when enabled")
        if conv_cap <= 0:
            raise ValueError(f"{p}: conviction_sizing_cap must be > 0 when conviction_sizing_enabled")

    return StrategySettings(
        guru_wallet_address=gw,
        token_filter=token_filter,
        copy_scale=scale,
        strategy_dedup_state_path=dedup_s,
        conviction_sizing_enabled=conv_en,
        conviction_sizing_cap=conv_cap,
        conviction_sizing_lookback_trades=lookback,
        min_follow_notional_usd=min_follow,
    )


def load_risk_settings(path: str | Path) -> RiskSettings:
    p = Path(path)
    raw = _root(yaml.safe_load(p.read_text(encoding="utf-8")), p)
    mqty = raw.get("max_order_quantity")
    if mqty is None:
        raise ValueError(f"{p}: max_order_quantity is required")
    max_order_quantity = float(mqty)
    if max_order_quantity <= 0:
        raise ValueError(f"{p}: max_order_quantity must be positive")

    mn = raw.get("max_notional_usd_per_order")
    if mn is None:
        raise ValueError(f"{p}: max_notional_usd_per_order is required")
    max_notional_usd_per_order = float(mn)
    if max_notional_usd_per_order <= 0:
        raise ValueError(f"{p}: max_notional_usd_per_order must be positive")

    mt = raw.get("max_token_notional_usd_open")
    max_token = float("inf") if mt is None else float(mt)
    if max_token <= 0:
        raise ValueError(f"{p}: max_token_notional_usd_open must be positive or null (unlimited)")

    kill_switch = bool(raw.get("kill_switch", False))
    fail_on_missing_price_for_notional = bool(
        raw.get("fail_on_missing_price_for_notional", True)
    )

    capital_gate = bool(raw.get("capital_gate_enabled", False))
    acct_age = float(raw.get("max_account_snapshot_age_seconds", 30.0))
    if acct_age < 0:
        raise ValueError(f"{p}: max_account_snapshot_age_seconds must be >= 0")
    allow_age = float(raw.get("max_allowance_snapshot_age_seconds", 120.0))
    if allow_age < 0:
        raise ValueError(f"{p}: max_allowance_snapshot_age_seconds must be >= 0")

    mc_raw = raw.get("min_collateral_balance_usd")
    min_collateral = None if mc_raw is None else float(mc_raw)

    ma_raw = raw.get("min_allowance_usd")
    min_allow = None if ma_raw is None else float(ma_raw)

    fail_unresolved = bool(raw.get("fail_on_unresolved_position_for_token_cap", False))

    mp_raw = raw.get("max_portfolio_notional_usd_open")
    max_portfolio = float("inf") if mp_raw is None else float(mp_raw)
    if not math.isinf(max_portfolio) and max_portfolio <= 0:
        raise ValueError(
            f"{p}: max_portfolio_notional_usd_open must be positive or null/omitted (unlimited)",
        )

    fail_portfolio_mark = bool(raw.get("fail_on_unresolved_portfolio_exposure", True))

    mc_raw = raw.get("max_concurrent_guru_resting_orders")
    max_conc: int | None
    if mc_raw is None:
        max_conc = None
    else:
        max_conc = int(mc_raw)
        if max_conc < 1:
            raise ValueError(
                f"{p}: max_concurrent_guru_resting_orders must be null/omitted (off) "
                "or an integer >= 1",
            )

    collateral_reserve = float(raw.get("collateral_reserve_usd", 0.0))
    if collateral_reserve < 0:
        raise ValueError(f"{p}: collateral_reserve_usd must be >= 0")
    if collateral_reserve > 0 and not capital_gate:
        raise ValueError(
            f"{p}: collateral_reserve_usd > 0 requires capital_gate_enabled: true",
        )

    return RiskSettings(
        max_order_quantity=max_order_quantity,
        max_notional_usd_per_order=max_notional_usd_per_order,
        max_token_notional_usd_open=max_token,
        kill_switch=kill_switch,
        fail_on_missing_price_for_notional=fail_on_missing_price_for_notional,
        capital_gate_enabled=capital_gate,
        max_account_snapshot_age_seconds=acct_age,
        max_allowance_snapshot_age_seconds=allow_age,
        min_collateral_balance_usd=min_collateral,
        min_allowance_usd=min_allow,
        fail_on_unresolved_position_for_token_cap=fail_unresolved,
        max_portfolio_notional_usd_open=max_portfolio,
        fail_on_unresolved_portfolio_exposure=fail_portfolio_mark,
        max_concurrent_guru_resting_orders=max_conc,
        collateral_reserve_usd=collateral_reserve,
    )


def phase_b_framework_truth_gates_active(risk: RiskSettings) -> bool:
    """True if any Phase B gate is configured that requires Nautilus ``Cache`` framework truth."""
    portfolio_on = not math.isinf(risk.max_portfolio_notional_usd_open)
    conc_on = risk.max_concurrent_guru_resting_orders is not None
    return portfolio_on or conc_on


def framework_phase_b_eligible(runtime: RuntimeSettings) -> bool:
    """True iff runtime can support Phase B framework-truth gates (Phase B plan §7.1)."""
    return (
        runtime.execution_mode == "live"
        and runtime.polymarket_nautilus_live
        and runtime.polymarket_framework_submit
    )


def validate_phase_b_runtime_contract(risk: RiskSettings, runtime: RuntimeSettings) -> None:
    """
    Reject unsupported risk/runtime combinations for Phase B (plan §§6–7).

    Call from :func:`tyrex_pm.runtime.guru_compose.build_guru_trading_node` so
    invalid assemblies fail at startup with explicit :class:`ValueError`.

    **Not** a substitute for :func:`load_risk_settings` checks that need no runtime
    (e.g. reserve vs ``capital_gate_enabled``).
    """
    if runtime.execution_mode == "shadow":
        if risk.collateral_reserve_usd > 0:
            raise ValueError(
                "collateral_reserve_usd > 0 is invalid when execution_mode is shadow "
                "(no py-clob collateral snapshot path on the guru node)",
            )
        if phase_b_framework_truth_gates_active(risk):
            raise ValueError(
                "Phase B framework-truth gates are invalid when execution_mode is shadow "
                "(finite max_portfolio_notional_usd_open and/or "
                "max_concurrent_guru_resting_orders set)",
            )

    if phase_b_framework_truth_gates_active(risk) and not framework_phase_b_eligible(runtime):
        raise ValueError(
            "Phase B framework-truth gates require execution_mode=live, "
            "polymarket_nautilus_live=true, polymarket_framework_submit=true "
            f"(got mode={runtime.execution_mode!r}, "
            f"polymarket_nautilus_live={runtime.polymarket_nautilus_live}, "
            f"polymarket_framework_submit={runtime.polymarket_framework_submit})",
        )


def load_runtime_settings(path: str | Path) -> RuntimeSettings:
    p = Path(path)
    raw = _root(yaml.safe_load(p.read_text(encoding="utf-8")), p)
    tid = str(raw.get("trader_id") or "").strip()
    if not tid or "-" not in tid:
        raise ValueError(f"{p}: trader_id must look like NAME-001")

    mode = str(raw.get("execution_mode", "shadow")).lower().strip()
    if mode not in ("shadow", "live"):
        raise ValueError(f"{p}: execution_mode must be shadow or live")

    poll = float(raw.get("guru_poll_interval_seconds", 30.0))
    if poll <= 0:
        raise ValueError(f"{p}: guru_poll_interval_seconds must be positive")

    api = str(raw.get("data_api_base_url", "https://data-api.polymarket.com")).rstrip("/")
    dedup = raw.get("guru_dedup_state_path")
    dedup_s = str(dedup).strip() if dedup else "var/guru_dedup.json"

    state = raw.get("guru_state_path")
    state_s = str(state).strip() if state else "var/guru_watermark.json"

    activity_limit = int(raw.get("guru_activity_limit", 200))
    if not (1 <= activity_limit <= 500):
        raise ValueError(f"{p}: guru_activity_limit must be between 1 and 500")

    backfill = float(raw.get("guru_startup_backfill_seconds", 0.0))
    if backfill < 0:
        raise ValueError(f"{p}: guru_startup_backfill_seconds must be >= 0")

    max_pages = int(raw.get("guru_max_activity_pages_per_poll", 4))
    if not (1 <= max_pages <= 20):
        raise ValueError(f"{p}: guru_max_activity_pages_per_poll must be between 1 and 20")

    log_level = str(raw.get("logging_level", "INFO")).upper()
    clob = str(raw.get("clob_host", "https://clob.polymarket.com")).rstrip("/")
    chain_id = int(raw.get("chain_id", 137))

    pnl = bool(raw.get("polymarket_nautilus_live", False))
    pfs = bool(raw.get("polymarket_framework_submit", False))
    pdi = bool(raw.get("polymarket_dynamic_instruments", False))

    inst_raw = raw.get("polymarket_instrument_ids")
    if inst_raw is None:
        inst_raw = []
    if not isinstance(inst_raw, list):
        raise ValueError(f"{p}: polymarket_instrument_ids must be a list of strings")
    poly_ids = tuple(str(x).strip() for x in inst_raw if str(x).strip())

    # **Package-source-confirmed:** ``PolymarketInstrumentProvider.load_ids_async`` no-ops when
    # ``instrument_ids`` is empty (logs "No instrument IDs given for loading"). Base
    # ``InstrumentProvider.initialize`` then warns and returns without loading — true
    # zero-bootstrap for ``load_ids``.
    if pnl and mode == "live" and not poly_ids:
        if not pfs:
            raise ValueError(
                f"{p}: polymarket_nautilus_live with empty polymarket_instrument_ids requires "
                "polymarket_framework_submit when execution_mode is live",
            )
        pdi = True  # **Repo-confirmed:** guru framework-submit + empty YAML ⇒ dynamic universe

    if pfs and mode == "live":
        if not pnl:
            raise ValueError(
                f"{p}: polymarket_framework_submit requires polymarket_nautilus_live "
                "when execution_mode is live",
            )

    if pdi and mode == "live":
        if not pnl:
            raise ValueError(
                f"{p}: polymarket_dynamic_instruments requires polymarket_nautilus_live "
                "when execution_mode is live",
            )
        if not pfs:
            raise ValueError(
                f"{p}: polymarket_dynamic_instruments requires polymarket_framework_submit "
                "when execution_mode is live",
            )

    dyn_max = int(raw.get("polymarket_dynamic_max_activations", 32))
    if dyn_max < 0:
        raise ValueError(f"{p}: polymarket_dynamic_max_activations must be >= 0")

    gamma_url = str(
        raw.get("polymarket_gamma_base_url", "https://gamma-api.polymarket.com"),
    ).rstrip("/")
    gamma_timeout = float(raw.get("polymarket_gamma_http_timeout_seconds", 15.0))
    if gamma_timeout <= 0:
        raise ValueError(f"{p}: polymarket_gamma_http_timeout_seconds must be positive")

    warmup_max = int(raw.get("polymarket_startup_token_warmup_max", 32))
    if warmup_max < 0:
        raise ValueError(f"{p}: polymarket_startup_token_warmup_max must be >= 0")

    token_map = _polymarket_token_instrument_map(poly_ids, path=p) if poly_ids else ()

    ingest_mode = str(raw.get("guru_ingest_mode", "poll_only")).lower().strip()
    if ingest_mode not in ("poll_only", "rtds_shadow", "rtds_primary"):
        raise ValueError(
            f"{p}: guru_ingest_mode must be poll_only, rtds_shadow, or rtds_primary",
        )
    ingest_phase = str(raw.get("guru_ingest_phase", "0")).strip()
    rtds_url = str(raw.get("guru_rtds_url", "wss://ws-live-data.polymarket.com")).strip()
    rtds_live = float(raw.get("guru_rtds_liveness_timeout_seconds", 120.0))
    if rtds_live <= 0:
        raise ValueError(f"{p}: guru_rtds_liveness_timeout_seconds must be positive")
    rtds_r0 = float(raw.get("guru_rtds_reconnect_retry_initial_seconds", 1.0))
    rtds_rmax = float(raw.get("guru_rtds_reconnect_retry_max_seconds", 60.0))
    if rtds_r0 <= 0 or rtds_rmax < rtds_r0:
        raise ValueError(f"{p}: invalid RTDS reconnect backoff seconds")
    rtds_ping = float(raw.get("guru_rtds_ping_interval_seconds", 5.0))
    if rtds_ping <= 0:
        raise ValueError(f"{p}: guru_rtds_ping_interval_seconds must be positive")
    poll_fb_en = bool(raw.get("guru_poll_fallback_enabled", True))
    poll_fb_iv = raw.get("guru_poll_fallback_interval_seconds")
    poll_fb_interval = float(poll_fb_iv) if poll_fb_iv is not None else None
    if poll_fb_interval is not None and poll_fb_interval <= 0:
        raise ValueError(f"{p}: guru_poll_fallback_interval_seconds must be positive or omitted")
    gap_fill_en = bool(raw.get("guru_gap_fill_enabled", True))
    gap_lb = float(raw.get("guru_gap_fill_lookback_seconds", 60.0))
    if gap_lb < 0:
        raise ValueError(f"{p}: guru_gap_fill_lookback_seconds must be >= 0")
    proxy_val = bool(raw.get("guru_proxy_wallet_validation_required", False))
    drain_ms = int(raw.get("guru_stream_queue_drain_interval_ms", 50))
    if drain_ms < 10:
        raise ValueError(f"{p}: guru_stream_queue_drain_interval_ms must be >= 10")

    ex_norm = bool(raw.get("execution_venue_normalize_enabled", False))
    ex_guard = bool(raw.get("execution_entry_guard_enabled", False))
    ex_slip_ticks = int(raw.get("execution_max_entry_slippage_ticks", 0))
    ex_depth = bool(raw.get("execution_book_depth_clip_enabled", False))
    ex_cap = float(raw.get("execution_book_depth_utilization_cap", 1.0))
    ex_rest = bool(raw.get("execution_book_rest_snapshot_enabled", False))
    ex_strict = bool(raw.get("execution_book_strict", False))
    ex_to_en = bool(raw.get("execution_limit_timeout_enabled", False))
    ex_to_s = float(raw.get("execution_limit_timeout_seconds", 30.0))

    if ex_slip_ticks < 0:
        raise ValueError(f"{p}: execution_max_entry_slippage_ticks must be >= 0")
    if ex_guard and ex_slip_ticks <= 0:
        raise ValueError(
            f"{p}: execution_entry_guard_enabled requires execution_max_entry_slippage_ticks > 0",
        )
    if ex_depth and not (0.0 < ex_cap <= 1.0 + 1e-9):
        raise ValueError(
            f"{p}: execution_book_depth_utilization_cap must be in (0, 1] when depth clip enabled",
        )
    if ex_to_en and ex_to_s <= 0:
        raise ValueError(
            f"{p}: execution_limit_timeout_seconds must be positive when timeout enabled",
        )

    reporting_en = bool(raw.get("reporting_enabled", False))
    reporting_base = str(raw.get("reporting_base_dir", "var/reporting/runs")).strip()
    if not reporting_base or ".." in reporting_base:
        raise ValueError(f"{p}: reporting_base_dir must be non-empty without '..'")
    r_max_q = int(raw.get("reporting_sink_max_queue", 50_000))
    if r_max_q < 100:
        raise ValueError(f"{p}: reporting_sink_max_queue must be >= 100")
    r_batch = int(raw.get("reporting_sink_batch_size", 128))
    if r_batch < 1:
        raise ValueError(f"{p}: reporting_sink_batch_size must be >= 1")

    return RuntimeSettings(
        trader_id=tid,
        execution_mode=mode,
        guru_poll_interval_seconds=poll,
        data_api_base_url=api,
        guru_dedup_state_path=dedup_s,
        guru_state_path=state_s,
        guru_activity_limit=activity_limit,
        guru_startup_backfill_seconds=backfill,
        guru_max_activity_pages_per_poll=max_pages,
        logging_level=log_level,
        clob_host=clob,
        chain_id=chain_id,
        polymarket_nautilus_live=pnl,
        polymarket_instrument_ids=poly_ids,
        polymarket_framework_submit=pfs,
        polymarket_token_to_instrument=token_map,
        polymarket_dynamic_instruments=pdi,
        polymarket_dynamic_max_activations=dyn_max,
        polymarket_gamma_base_url=gamma_url,
        polymarket_gamma_http_timeout_seconds=gamma_timeout,
        polymarket_startup_token_warmup_max=warmup_max,
        guru_ingest_mode=ingest_mode,
        guru_ingest_phase=ingest_phase,
        guru_rtds_url=rtds_url,
        guru_rtds_liveness_timeout_seconds=rtds_live,
        guru_rtds_reconnect_retry_initial_seconds=rtds_r0,
        guru_rtds_reconnect_retry_max_seconds=rtds_rmax,
        guru_rtds_ping_interval_seconds=rtds_ping,
        guru_poll_fallback_enabled=poll_fb_en,
        guru_poll_fallback_interval_seconds=poll_fb_interval,
        guru_gap_fill_enabled=gap_fill_en,
        guru_gap_fill_lookback_seconds=gap_lb,
        guru_proxy_wallet_validation_required=proxy_val,
        guru_stream_queue_drain_interval_ms=drain_ms,
        execution_venue_normalize_enabled=ex_norm,
        execution_entry_guard_enabled=ex_guard,
        execution_max_entry_slippage_ticks=ex_slip_ticks,
        execution_book_depth_clip_enabled=ex_depth,
        execution_book_depth_utilization_cap=ex_cap,
        execution_book_rest_snapshot_enabled=ex_rest,
        execution_book_strict=ex_strict,
        execution_limit_timeout_enabled=ex_to_en,
        execution_limit_timeout_seconds=ex_to_s,
        reporting_enabled=reporting_en,
        reporting_base_dir=reporting_base,
        reporting_sink_max_queue=r_max_q,
        reporting_sink_batch_size=r_batch,
    )
