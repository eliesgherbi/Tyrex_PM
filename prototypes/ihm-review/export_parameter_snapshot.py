"""Read the current schemas/scenarios into design fixtures. Does not run a workflow."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import yaml

from tyrex_pm.strategies.settlement_value.config import SettlementValueConfig
from tyrex_pm.strategies.settlement_value.schema import (
    settlement_value_config_as_parameters,
    settlement_value_config_from_parameters,
)

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def flat(value, prefix=""):
    out = {}
    for name, item in value.items():
        key = f"{prefix}.{name}" if prefix else name
        if isinstance(item, dict):
            out.update(flat(item, key))
        else:
            out[key] = item
    return out


cfg = SettlementValueConfig()
defaults = flat(settlement_value_config_as_parameters(cfg, full=True))
fields = []
for section in dataclasses.fields(cfg):
    for field in dataclasses.fields(getattr(cfg, section.name)):
        value = getattr(getattr(cfg, section.name), field.name)
        fields.append({
            "key": f"{section.name}.{field.name}",
            "section": section.name,
            "type": "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float, Decimal)) else "list" if isinstance(value, tuple) else "text",
            "integer": isinstance(value, int) and not isinstance(value, bool),
        })

source = ROOT / "config/runs/settlement_value_leader_5c_240s_paper.yaml"
raw = yaml.safe_load(source.read_text(encoding="utf-8"))
yaml_params = flat(settlement_value_config_as_parameters(
    settlement_value_config_from_parameters(raw["strategy"]["parameters"]), full=True
))
scenarios = {}
for name in ("friction", "favorable"):
    file = ROOT / f"config/research/scenarios/{name}.yaml"
    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    scenarios[name] = {"values": flat(data), "source": str(file.relative_to(ROOT)).replace("\\", "/"), "hash": hashlib.sha256(file.read_bytes()).hexdigest()[:12]}

payload = {"captured": "2026-10-22", "defaults": defaults, "fields": fields,
           "yamlBase": {"name": "Leader 5¢ / 240s", "source": str(source.relative_to(ROOT)).replace("\\", "/"), "values": yaml_params},
           "scenarios": scenarios}
(HERE / "src/parameterSnapshot.ts").write_text(
    "// Generated read-only source snapshot. Regenerate with export_parameter_snapshot.py.\n"
    + "export const parameterSnapshot = " + json.dumps(payload, indent=2) + ";\n",
    encoding="utf-8",
)
print(f"Captured {len(fields)} strategy fields and {len(scenarios)} scenarios for design review.")
