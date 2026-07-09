#!/usr/bin/env python3
"""Preflight validation for Phase 1 advisory live paired-binary scenarios."""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tyrex_pm.runtime.config import load_app_config  # noqa: E402

_PLACEHOLDER_MARKERS = (
    "<required",
    "<required_",
    "placeholder",
    "todo",
    "fixme",
    "changeme",
    "xxx",
    "yyyymmdd",
)
_SHADOW_MARKET_IDS = frozenset({"shadow_test_market", "test_market", "fixture_market"})
_BTC_MARKET_RE = re.compile(r"^btc_5m_\d{8}_\d{4}$", re.IGNORECASE)


def _is_placeholder(value: object) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    lower = text.lower()
    return any(marker in lower for marker in _PLACEHOLDER_MARKERS)


def _enforce_mode_count(app) -> tuple[int, list[str]]:
    entries = (
        ("reachability", app.survival.reachability.enforcement_mode),
        ("stall_exit", app.survival.stall_exit.enforcement_mode),
        ("trailing_stop", app.survival.trailing_stop.enforcement_mode),
        ("economics", app.survival.economics.enforcement_mode),
    )
    enforced = [name for name, mode in entries if str(mode).lower() == "enforce"]
    return len(enforced), enforced


def _phase1_profile_kind(scenario_name: str) -> str:
    name = scenario_name.lower()
    if name.endswith("_trailing_enforce"):
        return "trailing_enforce"
    if name.endswith("_stall_enforce"):
        return "stall_enforce"
    if name.endswith("_target_only"):
        return "target_only"
    return "advisory"


def validate_phase1_live_app(app, *, scenario_name: str = "", now_ts: float | None = None) -> list[str]:
    """Return list of validation errors (empty if OK)."""
    errors: list[str] = []
    now = now_ts if now_ts is not None else time.time()

    pb = app.paired_binary
    if pb is None:
        errors.append("paired_binary strategy config missing")
        return errors

    if app.runtime.execution_mode.value != "live":
        errors.append(f"execution_mode must be live (got {app.runtime.execution_mode.value})")

    market_id = str(pb.market_id or "").strip()
    if not market_id or market_id in _SHADOW_MARKET_IDS:
        errors.append(f"market_id must be a real live market id (got {market_id!r})")
    elif _is_placeholder(market_id):
        errors.append(f"market_id looks like a placeholder ({market_id!r})")

    condition_id = pb.condition_id
    if _is_placeholder(condition_id):
        errors.append("condition_id is required and must not be a placeholder")

    for field_name, token in (
        ("yes_token_id", pb.yes_token_id),
        ("no_token_id", pb.no_token_id),
    ):
        if _is_placeholder(token):
            errors.append(f"{field_name} is required and must not be a placeholder")

    start_ts = pb.event_start_ts
    end_ts = pb.event_end_ts
    if start_ts is None:
        errors.append("event_start_ts is required")
    if end_ts is None:
        errors.append("event_end_ts is required")
    if start_ts is not None and end_ts is not None:
        if end_ts <= start_ts:
            errors.append(f"event_end_ts ({end_ts}) must be > event_start_ts ({start_ts})")
        if end_ts < now - 300:
            errors.append(
                f"event_end_ts ({end_ts}) is more than 5 minutes in the past "
                f"(now={now:.0f}); pin a current/live market window"
            )
        if end_ts > now + 7200:
            errors.append(
                f"event_end_ts ({end_ts}) is more than 2 hours in the future "
                f"(now={now:.0f})"
            )

    sl = app.runtime.strategy_lifecycle
    if not sl.enabled:
        errors.append("runtime.strategy_lifecycle must be enabled (market_aware)")
    if sl.max_runtime_s is not None:
        errors.append("runtime.strategy_lifecycle.max_runtime_s must be null for Phase 1 live")

    if not app.survival.enabled:
        errors.append("survival.enabled must be true for Phase 1 advisory live")
    enforce_count, enforced = _enforce_mode_count(app)
    profile = _phase1_profile_kind(scenario_name)
    if enforce_count > 1:
        errors.append(
            f"only one survival enforce module allowed (found {enforce_count}: {enforced})"
        )
    if "economics" in enforced:
        errors.append("survival.economics.enforcement_mode=enforce is not allowed for Phase 1 live tests")
    if profile in {"trailing_enforce", "stall_enforce"}:
        expected = "trailing_stop" if profile == "trailing_enforce" else "stall_exit"
        if enforce_count != 1 or enforced[0] != expected:
            errors.append(
                f"profile {profile} requires exactly survival.{expected}.enforcement_mode=enforce"
            )
    elif enforce_count > 0:
        errors.append(
            f"advisory/target-only profile cannot enable enforce modes (found {enforced})"
        )
    if str(app.survival.target_policy.mode).lower() not in {"dynamic", "full_recovery"}:
        errors.append("survival.target_policy.mode should be dynamic for Phase 1")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1 advisory live scenario preflight")
    parser.add_argument(
        "--scenario",
        default="live_paired_binary_phase1_tiny",
        help="Scenario name under config/scenarios/ (without .yaml)",
    )
    parser.add_argument(
        "--strategy",
        default="config/strategies/paired_binary.yaml",
        help="Strategy YAML path relative to repo root",
    )
    parser.add_argument(
        "--event-url",
        default=None,
        metavar="URL_OR_SLUG",
        help=(
            "Polymarket event URL or slug. Resolves market metadata from Gamma and "
            "fills missing/placeholder paired_binary fields before validation."
        ),
    )
    args = parser.parse_args()

    scenario_path = REPO / "config" / "scenarios" / f"{args.scenario}.yaml"
    if not scenario_path.is_file():
        print(f"ERROR: scenario not found: {scenario_path}")
        return 1

    app = load_app_config(
        repo_root=REPO,
        strategy_file=args.strategy,
        scenario_file=str(scenario_path.relative_to(REPO)),
    )
    if args.event_url or app.paired_binary is not None:
        from tyrex_pm.runtime.paired_binary_metadata import resolve_and_apply_paired_binary_metadata
        from tyrex_pm.venue.polymarket.event_metadata import EventMetadataError, EventMetadataLookupError

        try:
            app, resolved = resolve_and_apply_paired_binary_metadata(app, event_url=args.event_url)
        except (EventMetadataError, EventMetadataLookupError) as exc:
            print(f"ERROR: event metadata resolution failed: {exc}")
            return 1
        except httpx.HTTPError as exc:
            print(f"ERROR: event metadata HTTP error: {exc!r}")
            return 1
        if resolved is not None:
            print(f"Resolved metadata from {args.event_url or 'token_ids'}")
    errors = validate_phase1_live_app(app, scenario_name=args.scenario)
    if errors:
        print("Phase 1 live preflight FAILED:")
        for err in errors:
            print(f"  - {err}")
        return 1

    pb = app.paired_binary
    assert pb is not None
    print("Phase 1 live preflight OK")
    print(f"  market_id: {pb.market_id}")
    print(f"  condition_id: {pb.condition_id}")
    print(f"  event_start_ts: {pb.event_start_ts}")
    print(f"  event_end_ts: {pb.event_end_ts}")
    print(f"  yes_token_id: ...{str(pb.yes_token_id)[-8:]}")
    print(f"  no_token_id: ...{str(pb.no_token_id)[-8:]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
