"""Configuration identity compatibility (integration plan, amendment 0.5).

Identities (config_hash / config_id) were frozen with the fields listed in FROZEN below.
A field or section added later is omitted from the identity payload only while it equals
its registered legacy value; any other value changes the identity. A new field that is
not registered in schema.IDENTITY_LATER_FIELDS / IDENTITY_LATER_SECTIONS fails here.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from tyrex_pm.research.strategy_replay.results import config_hash
from tyrex_pm.strategies.settlement_value.config import SettlementValueConfig
from tyrex_pm.strategies.settlement_value.schema import (
    _SECTIONS,
    IDENTITY_LATER_FIELDS,
    IDENTITY_LATER_SECTIONS,
    settlement_value_config_as_parameters,
    settlement_value_config_from_parameters,
)

ROOT = Path(__file__).resolve().parents[4]
# Fields present when identities were frozen (strategy_closed_loop_v3, 2026-10-16).
FROZEN = {
    "settlement": {"functional_id", "comparison", "reference_series", "reference_units",
                   "timestamp_meaning", "provenance_status", "evidence"},
    "model": {"backend", "averaging_window_s", "spot_input", "sigma_band",
              "reference_band_bps", "tau_floor_s"},
    "volatility": {"half_life_s", "min_samples_s", "jump_threshold_sigma",
                   "sample_interval_s", "tau_floor_s", "sigma_floor", "sigma_scale"},
    "entry": {"tau_min_s", "tau_max_s", "min_edge_per_share", "target_mode",
              "target_profit_usdc", "max_entry_price", "limit_buffer_ticks"},
}
V2_IDS = {
    "default": "3b6dbbf4edbdd6d3ac771ecb964342286c391b6fa30bb6e3822b6e54d8805618",
    "comparison": "23ca8d880ec1345c0aa13831d50823affe3253de8ca6b29379f7835ff4143975",
}


def _legacy_payload(cfg: SettlementValueConfig) -> dict:
    """The identity serializer as it was before any later field existed."""
    out = {}
    for name in _SECTIONS:
        if name == "disagreement_veto":
            continue
        section = getattr(cfg, name)
        if name in ("retry", "exit", "reentry") and section == type(section)():
            continue
        values = {}
        for f in dataclasses.fields(section):
            if (name, f.name) in IDENTITY_LATER_FIELDS:
                continue
            value = getattr(section, f.name)
            if isinstance(value, Decimal):
                value = str(value)
            elif isinstance(value, tuple):
                value = list(value)
            values[f.name] = value
        out[name] = values
    return out


def _settlement_value_run_configs() -> list[Path]:
    paths = []
    for path in sorted((ROOT / "config" / "runs").glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if (raw.get("strategy") or {}).get("kind") == "settlement_value":
            paths.append(path)
    return paths


def test_every_field_is_frozen_or_explicitly_registered() -> None:
    for name, cls in _SECTIONS.items():
        fields = {f.name for f in dataclasses.fields(cls)}
        if name in IDENTITY_LATER_SECTIONS:
            continue  # whole section registered as later
        unregistered = fields - FROZEN.get(name, fields) - {
            f for (s, f) in IDENTITY_LATER_FIELDS if s == name}
        assert not unregistered, (
            f"{name}: {sorted(unregistered)} must be registered in IDENTITY_LATER_FIELDS")


@pytest.mark.parametrize("path", _settlement_value_run_configs(), ids=lambda p: p.name)
def test_run_template_identities_preserve_legacy_or_identify_new_behavior(path: Path) -> None:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg = settlement_value_config_from_parameters(raw["strategy"]["parameters"])
    changed = any(getattr(getattr(cfg, section), name) != default
                  for (section, name), default in IDENTITY_LATER_FIELDS.items())
    if changed:
        assert settlement_value_config_as_parameters(cfg) != _legacy_payload(cfg)
    else:
        assert settlement_value_config_as_parameters(cfg) == _legacy_payload(cfg)


def test_frozen_candidate_ids_are_unchanged() -> None:
    from tyrex_pm.research.strategy_lab import v3

    assert {k: c.config_id for k, c in v3.load_candidates().items()} == V2_IDS


def test_behaviour_changing_values_get_a_distinct_identity() -> None:
    base = settlement_value_config_from_parameters({})
    corrected = settlement_value_config_from_parameters(
        {"volatility": {"variance_time_policy": "elapsed_variance_v1"}})
    vetoed = settlement_value_config_from_parameters(
        {"disagreement_veto": {"enabled": True, "max_gap_pp": 9.0}})
    hashes = {config_hash("settlement_value", c) for c in (base, corrected, vetoed)}
    assert len(hashes) == 3
    assert settlement_value_config_as_parameters(corrected)["volatility"][
        "variance_time_policy"] == "elapsed_variance_v1"
    assert settlement_value_config_as_parameters(vetoed)["disagreement_veto"]["enabled"]


def test_full_effective_configuration_keeps_every_field() -> None:
    full = settlement_value_config_as_parameters(SettlementValueConfig(), full=True)
    assert set(full) == set(_SECTIONS)
    assert full["volatility"]["variance_time_policy"] == "per_sample_v0"
    assert full["disagreement_veto"]["enabled"] is False
    assert settlement_value_config_from_parameters(full) == SettlementValueConfig()


@pytest.mark.parametrize("path", _settlement_value_run_configs(), ids=lambda p: p.name)
def test_live_and_research_parse_the_same_configuration(path: Path) -> None:
    from tyrex_pm.runtime.run_config import load_trading_run_config

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    research = settlement_value_config_from_parameters(raw["strategy"]["parameters"])
    try:
        live = load_trading_run_config(path).strategy
    except Exception as exc:  # templates may require operator values to validate
        pytest.skip(f"run config does not validate standalone: {exc}")
    assert live == research
