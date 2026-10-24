"""Strict effective profiles and credential-only dotenv loading."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping, get_type_hints

import yaml

from .contracts import canonical, digest


@dataclass(frozen=True)
class Selector:
    venue: str = "polymarket"
    asset: str = "BTC"
    duration_s: int = 300
    slug_pattern: str = r"^btc-updown-5m-[0-9]+$"


@dataclass(frozen=True)
class Discovery:
    refresh_s: int = 15
    scan_interval_s: int = 300
    lookahead_s: int = 86400
    max_selected_markets: int = 512


@dataclass(frozen=True)
class Coverage:
    required_market_start: str = "window_start"
    required_market_end: str = "observed_trading_close"
    pre_window_capture_s: int = 300
    external_warmup_s: int = 900
    track_since_discovery: bool = True
    post_close_tail_s: int = 120
    # Book (CLOB WebSocket) subscriptions start this long before a market opens. Discovery
    # still looks lookahead_s ahead for metadata. Subscribing every discovered market grew
    # the socket to ~700 tokens over a session and the venue then closed it every few
    # minutes (6, 15 and 19 Oct recordings).
    book_subscribe_ahead_s: int = 900


@dataclass(frozen=True)
class Polymarket:
    capture_all_selected_market_messages: bool = True
    custom_feature_enabled: bool = True
    metadata_refresh_s: int = 60
    cold_metadata_refresh_s: int = 900
    rest_book_audit_s: int = 60
    rest_book_audit_batch_size: int = 500
    tokens_per_connection: int = 200
    metadata_max_age_s: int = 180


@dataclass(frozen=True)
class Reference:
    required: bool = True
    rule_policy: str = "validated_market_rules_v1"
    supported_feed: str = "chainlink_btcusd_twap_60s"
    credentials_env_prefix: str = "TYREX_DATA_PM"
    liveness_s: int = 15
    observation_warn_s: int = 5


@dataclass(frozen=True)
class Binance:
    required: bool = True
    symbol: str = "BTCUSDT"
    trades: str = "individual"
    depth: str = "diff_100ms"
    bootstrap_levels: int = 5000


@dataclass(frozen=True)
class External:
    binance_spot: Binance = field(default_factory=Binance)


@dataclass(frozen=True)
class Resolution:
    poll_initial_s: int = 15
    poll_after_10m_s: int = 60
    poll_after_24h_s: int = 900
    pending_retention: str = "until_resolved_or_explicitly_retired"


@dataclass(frozen=True)
class Storage:
    raw_rotate_s: int = 60
    raw_rotate_mib: int = 64
    raw_fsync_interval_ms: int = 250
    capture_queue_mib: int = 64
    min_free_disk_gib: int = 5
    compression: str = "zstd"
    normalized_target_mib: int = 128


@dataclass(frozen=True)
class Operations:
    requests_per_second: int = 2
    request_burst: int = 4
    binance_weight_per_minute: int = 1200
    connect_timeout_s: int = 5
    read_timeout_s: int = 10
    reconnect_max_s: int = 30
    shutdown_timeout_s: int = 30
    heartbeat_s: int = 5
    quality_interval_s: int = 30


@dataclass(frozen=True)
class Profile:
    schema_version: int = 1
    profile_id: str = "btc-5m"
    profile_version: int = 1
    family: str = "recurring_price_window"
    selector: Selector = field(default_factory=Selector)
    discovery: Discovery = field(default_factory=Discovery)
    coverage: Coverage = field(default_factory=Coverage)
    polymarket: Polymarket = field(default_factory=Polymarket)
    reference: Reference = field(default_factory=Reference)
    external: External = field(default_factory=External)
    resolution: Resolution = field(default_factory=Resolution)
    storage: Storage = field(default_factory=Storage)
    operations: Operations = field(default_factory=Operations)

    @property
    def hash(self) -> str:
        return digest(canonical(asdict(self)))

    def validate(self) -> None:
        if self.schema_version != 1 or self.profile_version < 1:
            raise ValueError("unsupported profile version")
        if (self.family, self.selector.venue, self.selector.asset, self.selector.duration_s) != (
            "recurring_price_window",
            "polymarket",
            "BTC",
            300,
        ):
            raise ValueError("live v1 supports BTC five-minute collection only")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.profile_id):
            raise ValueError("invalid profile identity")
        re.compile(self.selector.slug_pattern)
        if self.reference.supported_feed != "chainlink_btcusd_twap_60s":
            raise ValueError("unsupported required reference")
        if self.reference.credentials_env_prefix != "TYREX_DATA_PM":
            raise ValueError("credential prefix must be TYREX_DATA_PM")
        b = self.external.binance_spot
        if (b.symbol, b.trades, b.depth, b.bootstrap_levels) != (
            "BTCUSDT",
            "individual",
            "diff_100ms",
            5000,
        ):
            raise ValueError("unsupported Binance contract")
        if self.storage.compression != "zstd":
            raise ValueError("unsupported compression")
        if not self.polymarket.capture_all_selected_market_messages:
            raise ValueError("raw collection cannot discard selected-market messages")
        if not 1 <= self.polymarket.rest_book_audit_batch_size <= 500:
            raise ValueError("book batch must be between 1 and 500")
        for section in asdict(self).values():
            if isinstance(section, dict):
                for name, value in section.items():
                    if type(value) is int and value < (0 if name == "min_free_disk_gib" else 1):
                        raise ValueError(f"invalid positive setting {name}")
        if self.coverage.book_subscribe_ahead_s < self.coverage.pre_window_capture_s:
            raise ValueError("books must be subscribed at least pre_window_capture_s ahead")
        if self.coverage.required_market_start != "window_start" or (
            self.coverage.required_market_end != "observed_trading_close"
        ):
            raise ValueError("unsupported coverage boundary policy")
        if self.resolution.pending_retention != "until_resolved_or_explicitly_retired":
            raise ValueError("unresolved obligations cannot expire automatically")
        count = min(
            self.discovery.max_selected_markets,
            self.discovery.lookahead_s // self.selector.duration_s + 2,
        )
        hot = min(3, count)
        # Two metadata endpoints, audit batches, discovery, and 30% retry/settlement reserve.
        steady = (
            2 * (count - hot) / self.polymarket.cold_metadata_refresh_s
            + 2 * hot / self.polymarket.metadata_refresh_s
            + (2 * count / self.polymarket.rest_book_audit_batch_size + 1)
            / self.polymarket.rest_book_audit_s
            + 4 / self.discovery.refresh_s
            + 30 / self.discovery.scan_interval_s
        )
        if steady >= self.operations.requests_per_second * 0.7:
            raise ValueError("required schedule exceeds conservative request budget")


def strict(cls: type, value: Mapping[str, Any]) -> Any:
    if not isinstance(value, dict):
        raise ValueError(f"{cls.__name__} requires a mapping")
    hints = get_type_hints(cls)
    unknown = set(value) - {f.name for f in fields(cls)}
    if unknown:
        raise ValueError(f"unknown {cls.__name__} keys: {sorted(unknown)}")
    result = {}
    for key, item in value.items():
        expected = hints[key]
        if is_dataclass(expected):
            result[key] = strict(expected, item)
        elif type(item) is not expected:
            raise ValueError(f"{key} requires {expected.__name__}")
        else:
            result[key] = item
    return cls(**result)


def load_profile(path: Path | None = None) -> Profile:
    text = (
        path.read_text(encoding="utf-8")
        if path
        else files(__package__).joinpath("profiles/btc_5m.yaml").read_text(encoding="utf-8")
    )
    profile = strict(Profile, yaml.safe_load(text))
    profile.validate()
    return profile


@dataclass(frozen=True, repr=False)
class Credentials:
    key: str
    secret: str
    passphrase: str

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"

    def auth(self) -> dict[str, str]:
        return {"apiKey": self.key, "secret": self.secret, "passphrase": self.passphrase}


def load_credentials(
    dotenv: Path | None, environ: Mapping[str, str] | None = None, *, required: bool = True
) -> Credentials | None:
    """Read the API triplet only. No global environment mutation or wallet-key loading."""
    names = {
        f"{prefix}_{suffix}"
        for prefix in ("TYREX_DATA_PM", "POLYMARKET")
        for suffix in ("API_KEY", "API_SECRET", "API_PASSPHRASE")
    }
    names.add("POLYMARKET_PASSPHRASE")
    values = {}
    if dotenv and dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            name, separator, value = line.partition("=")
            name = name.strip()
            if separator and name in names:
                value = value.strip()
                if value.startswith(("'", '"')):
                    quote = value[0]
                    end = value.find(quote, 1)
                    if end < 0 or value[end + 1 :].strip().strip("# "):
                        # Do not interpolate shell expressions or expand other secrets.
                        if end < 0:
                            raise ValueError(f"invalid quoted credential for {name}")
                    value = value[1:end]
                else:
                    value = value.split(" #", 1)[0].strip()
                values[name] = value
    values.update(
        {k: v for k, v in (os.environ if environ is None else environ).items() if k in names}
    )
    prefix = (
        "TYREX_DATA_PM"
        if any(values.get(k) for k in names if k.startswith("TYREX_DATA_PM"))
        else "POLYMARKET"
    )
    triplet = [
        values.get(f"{prefix}_{suffix}", "").strip()
        for suffix in ("API_KEY", "API_SECRET", "API_PASSPHRASE")
    ]
    if prefix == "POLYMARKET" and not triplet[2]:
        triplet[2] = values.get("POLYMARKET_PASSPHRASE", "").strip()
    if all(triplet):
        return Credentials(*triplet)
    if required or any(triplet):
        raise ValueError(f"complete {prefix} API key/secret/passphrase triplet required")
    return None
