from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from tyrex_pm.core.enums import ExecutionMode
from tyrex_pm.core.errors import ConfigError


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _load_yaml(p: Path) -> dict[str, Any]:
    if not p.is_file():
        raise ConfigError(f"missing config: {p}")
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class GuruConfig:
    wallet: str
    data_api_poll_interval_s: float
    data_api_limit: int
    data_api_max_pages_per_poll: int


@dataclass(frozen=True)
class FiltersConfig:
    token_allowlist: frozenset[str]
    min_notional_usd: Decimal
    significance_min_notional_usd: Decimal
    min_conviction_score: Decimal
    exclude_untradeable_markets: bool


@dataclass(frozen=True)
class ConvictionConfig:
    enabled: bool
    score_min: Decimal
    score_max: Decimal
    min_multiplier: Decimal
    max_multiplier: Decimal


@dataclass(frozen=True)
class SizingConfig:
    copy_scale: Decimal
    conviction: ConvictionConfig
    #: BUY entry: fixed USD notional (ignores copy_scale and conviction when True).
    static_enabled: bool
    static_amount_usd: Decimal


@dataclass(frozen=True)
class ExitsConfig:
    dust_notional_usd: Decimal
    sell_mode: str  # proportional_to_guru | full_bot_position


@dataclass(frozen=True)
class StrategyConfig:
    guru: GuruConfig
    filters: FiltersConfig
    sizing: SizingConfig
    exits: ExitsConfig


@dataclass(frozen=True)
class NotionalConfig:
    min_usd: Decimal
    max_usd: Decimal
    #: When order notional exceeds max_usd: clip size to max (cap) or reject (deny).
    max_policy: str


@dataclass(frozen=True)
class DeploymentConfig:
    token_cap_usd: Decimal
    portfolio_cap_usd: Decimal


@dataclass(frozen=True)
class CapitalConfig:
    enabled: bool
    max_wallet_age_s: int


@dataclass(frozen=True)
class VenueMinSizeConfig:
    """Pre-submit guard: clip-side and notional-cap math can produce a final ``size`` below
    the venue's hard minimum (Polymarket commonly = 5 shares). Without this gate the order
    reaches the venue and is rejected with ``Size (X) lower than the minimum: 5``.

    ``policy=deny``  → block locally with reason ``below_venue_min_size`` (no submit).
    ``policy=bump``  → raise ``size`` to ``default_min_size`` then re-validate deployment +
    capital with the bumped size; only submit if both still pass.
    """

    enabled: bool
    policy: str  # "deny" | "bump"
    default_min_size: Decimal


@dataclass(frozen=True)
class InventoryConfig:
    sell_requires_venue_position: bool


@dataclass(frozen=True)
class KillSwitchConfig:
    enabled: bool


@dataclass(frozen=True)
class ConcurrencyConfig:
    max_orders_in_flight: int


@dataclass(frozen=True)
class ReadinessConfig:
    require_wallet_sync: bool
    max_wallet_age_s_live: int
    require_heartbeat_live: bool
    require_user_ws_live: bool


@dataclass(frozen=True)
class RiskConfig:
    notional: NotionalConfig
    deployment: DeploymentConfig
    capital: CapitalConfig
    inventory: InventoryConfig
    kill_switch: KillSwitchConfig
    concurrency: ConcurrencyConfig
    readiness: ReadinessConfig
    venue_min_size: VenueMinSizeConfig


@dataclass(frozen=True)
class ReportingConfig:
    enabled: bool
    runs_dir: str


@dataclass(frozen=True)
class ShadowBootstrapConfig:
    """Seed WalletStore for shadow runs when venue sync is not wired (not secrets)."""

    usdc_balance: Decimal
    usdc_allowance: Decimal


@dataclass(frozen=True)
class RuntimeConfig:
    execution_mode: ExecutionMode
    reporting: ReportingConfig
    reconcile_interval_s: int
    #: Provisional repair window (s): age below this is non-blocking ``provisional_pending_venue``.
    submit_grace_s: float
    #: Provisional age (s) past which absent rows auto-resolve to ``UNKNOWN_TERMINAL`` (non-blocking)
    #: when WS is fresh and no venue restart suspected.
    provisional_unknown_terminal_timeout_s: float
    #: Back-compat alias kept for old configs / scripts; mirrors ``provisional_unknown_terminal_timeout_s``.
    venue_confirm_provisional_timeout_s: float
    #: Adoption window (s) for venue-truth mirror race: a venue order id that we don't yet track
    #: locally is matched against recent no-vid provisional rows submitted within this window.
    adoption_grace_s: float
    log_level: str
    shadow_bootstrap: ShadowBootstrapConfig | None


@dataclass(frozen=True)
class AppConfig:
    strategy: StrategyConfig
    risk: RiskConfig
    runtime: RuntimeConfig
    raw: dict[str, Any]


def _dec(d: dict[str, Any], key: str, default: str = "0") -> Decimal:
    v = d.get(key, default)
    return Decimal(str(v))


def _parse_venue_min_size(d: dict[str, Any]) -> VenueMinSizeConfig:
    """Parse the ``risk.venue_min_size`` block (defaults: enabled, deny, 5 shares).

    Polymarket's hard floor is 5 shares regardless of token. ``5`` is therefore the safe
    default when the operator omits the block entirely. ``policy`` accepts ``deny|bump``
    and falls back to ``deny`` for any other string (fail-closed).
    """
    raw_policy = str(d.get("policy", "deny") or "deny").lower().strip()
    policy = raw_policy if raw_policy in ("deny", "bump") else "deny"
    return VenueMinSizeConfig(
        enabled=bool(d.get("enabled", True)),
        policy=policy,
        default_min_size=_dec(d, "default_min_size", "5"),
    )


def _parse_conviction(d: dict[str, Any]) -> ConvictionConfig:
    if not d:
        return ConvictionConfig(
            enabled=False,
            score_min=Decimal("0"),
            score_max=Decimal("1"),
            min_multiplier=Decimal("1"),
            max_multiplier=Decimal("1"),
        )
    return ConvictionConfig(
        enabled=bool(d.get("enabled", False)),
        score_min=_dec(d, "score_min", "0"),
        score_max=_dec(d, "score_max", "1"),
        min_multiplier=_dec(d, "min_multiplier", "0.5"),
        max_multiplier=_dec(d, "max_multiplier", "2.0"),
    )


def parse_app_config(*, risk: dict[str, Any], strategy: dict[str, Any], runtime: dict[str, Any]) -> AppConfig:
    g = strategy.get("guru") or {}
    f = strategy.get("filters") or {}
    sz = strategy.get("sizing") or {}
    ex = strategy.get("exits") or {}

    allow = f.get("token_allowlist") or []
    if not isinstance(allow, list):
        allow = []
    strat = StrategyConfig(
        guru=GuruConfig(
            wallet=str(g.get("wallet", "")),
            data_api_poll_interval_s=float(g.get("data_api_poll_interval_s", 5)),
            data_api_limit=int(g.get("data_api_limit", 50)),
            data_api_max_pages_per_poll=int(g.get("data_api_max_pages_per_poll", 5)),
        ),
        filters=FiltersConfig(
            token_allowlist=frozenset(str(x) for x in allow),
            min_notional_usd=_dec(f, "min_notional_usd", "0"),
            significance_min_notional_usd=_dec(f, "significance_min_notional_usd", "0"),
            min_conviction_score=_dec(f, "min_conviction_score", "-1000000000"),
            exclude_untradeable_markets=bool(f.get("exclude_untradeable_markets", False)),
        ),
        sizing=SizingConfig(
            copy_scale=_dec(sz, "copy_scale", "1"),
            conviction=_parse_conviction(sz.get("conviction") or {}),
            static_enabled=bool(sz.get("static_enabled", False)),
            static_amount_usd=_dec(sz, "static_amount_usd", "0"),
        ),
        exits=ExitsConfig(
            dust_notional_usd=_dec(ex, "dust_notional_usd", "0.5"),
            sell_mode=str(ex.get("sell_mode", "proportional_to_guru")),
        ),
    )

    n = risk.get("notional") or {}
    d = risk.get("deployment") or {}
    c = risk.get("capital") or {}
    inv = risk.get("inventory") or {}
    ks = risk.get("kill_switch") or {}
    co = risk.get("concurrency") or {}
    rd = risk.get("readiness") or {}
    vms = risk.get("venue_min_size") or {}

    mp_raw = str(n.get("max_policy", "deny") or "deny").lower().strip()
    max_policy = mp_raw if mp_raw in ("cap", "deny") else "deny"
    rsk = RiskConfig(
        notional=NotionalConfig(
            min_usd=_dec(n, "min_usd"),
            max_usd=_dec(n, "max_usd"),
            max_policy=max_policy,
        ),
        deployment=DeploymentConfig(
            token_cap_usd=_dec(d, "token_cap_usd"),
            portfolio_cap_usd=_dec(d, "portfolio_cap_usd"),
        ),
        capital=CapitalConfig(
            # Default True when key omitted — live guru-follow should pre-check BUY vs wallet.
            enabled=bool(c.get("enabled", True)),
            max_wallet_age_s=int(c.get("max_wallet_age_s", 120)),
        ),
        inventory=InventoryConfig(
            sell_requires_venue_position=bool(inv.get("sell_requires_venue_position", True)),
        ),
        kill_switch=KillSwitchConfig(enabled=bool(ks.get("enabled", False))),
        concurrency=ConcurrencyConfig(max_orders_in_flight=int(co.get("max_orders_in_flight", 8))),
        readiness=ReadinessConfig(
            require_wallet_sync=bool(rd.get("require_wallet_sync", True)),
            max_wallet_age_s_live=int(rd.get("max_wallet_age_s_live", 60)),
            require_heartbeat_live=bool(rd.get("require_heartbeat_live", True)),
            require_user_ws_live=bool(rd.get("require_user_ws_live", True)),
        ),
        venue_min_size=_parse_venue_min_size(vms),
    )

    em = str(runtime.get("execution_mode", "shadow")).lower()
    execution_mode = ExecutionMode.LIVE if em == "live" else ExecutionMode.SHADOW
    rep = runtime.get("reporting") or {}
    sup = runtime.get("supervisors") or {}
    log = runtime.get("logging") or {}
    sb_raw = runtime.get("shadow_bootstrap")
    shadow_boot: ShadowBootstrapConfig | None = None
    if isinstance(sb_raw, dict) and sb_raw:
        shadow_boot = ShadowBootstrapConfig(
            usdc_balance=_dec(sb_raw, "usdc_balance", "0"),
            usdc_allowance=_dec(sb_raw, "usdc_allowance", "0"),
        )

    submit_grace = float(sup.get("submit_grace_s", 15))
    # New canonical field; old field is honored as alias if user has not migrated.
    unknown_terminal = sup.get("provisional_unknown_terminal_timeout_s")
    if unknown_terminal is None:
        unknown_terminal = sup.get("venue_confirm_provisional_timeout_s", 60)
    unknown_terminal = float(unknown_terminal)
    adoption_grace = float(sup.get("adoption_grace_s", 5))
    rt = RuntimeConfig(
        execution_mode=execution_mode,
        reporting=ReportingConfig(
            enabled=bool(rep.get("enabled", True)),
            runs_dir=str(rep.get("runs_dir", "var/reporting/runs")),
        ),
        reconcile_interval_s=int(sup.get("reconcile_interval_s", 30)),
        submit_grace_s=submit_grace,
        provisional_unknown_terminal_timeout_s=unknown_terminal,
        venue_confirm_provisional_timeout_s=unknown_terminal,
        adoption_grace_s=adoption_grace,
        log_level=str(log.get("level", "INFO")),
        shadow_bootstrap=shadow_boot,
    )

    raw = {"risk": risk, "strategy": strategy, "runtime": runtime}
    return AppConfig(strategy=strat, risk=rsk, runtime=rt, raw=raw)


def _resolve_scenario_path(repo_root: Path, scenario_file: str | None) -> str | None:
    """Bare name `shadow_guru` → `config/scenarios/shadow_guru.yaml` under repo_root."""
    if scenario_file is None:
        return None
    raw = scenario_file.strip()
    p = Path(raw)
    if p.is_absolute():
        return raw
    if "/" in raw or "\\" in raw:
        return str(repo_root / raw)
    name = raw if raw.endswith(".yaml") else f"{raw}.yaml"
    return str(repo_root / "config" / "scenarios" / name)


def load_app_config(
    *,
    repo_root: Path,
    strategy_file: str = "config/strategies/guru_follow.yaml",
    scenario_file: str | None = None,
) -> AppConfig:
    scenario_file = _resolve_scenario_path(repo_root, scenario_file)
    risk_p = repo_root / "config" / "risk" / "default.yaml"
    rt_p = repo_root / "config" / "runtime" / "default.yaml"
    st_p = repo_root / strategy_file if not Path(strategy_file).is_absolute() else Path(strategy_file)

    risk = _load_yaml(risk_p)
    runtime = _load_yaml(rt_p)
    strategy = _load_yaml(st_p)

    if scenario_file:
        sc_p = repo_root / scenario_file if not Path(scenario_file).is_absolute() else Path(scenario_file)
        sc = _load_yaml(sc_p)
        if "risk" in sc:
            risk = _deep_merge(risk, sc["risk"])
        if "runtime" in sc:
            runtime = _deep_merge(runtime, sc["runtime"])
        if "strategy" in sc:
            strategy = _deep_merge(strategy, sc["strategy"])
        # scenario top-level keys
        rt_overlay = {k: sc[k] for k in ("execution_mode", "reporting", "supervisors", "logging") if k in sc}
        if rt_overlay:
            runtime = _deep_merge(runtime, rt_overlay)
        st_overlay = {k: sc[k] for k in ("guru", "filters", "sizing", "exits") if k in sc}
        if st_overlay:
            strategy = _deep_merge(strategy, st_overlay)

    return parse_app_config(risk=risk, strategy=strategy, runtime=runtime)
