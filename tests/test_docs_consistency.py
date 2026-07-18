"""Lightweight docs↔code consistency checks (not brittle prose locks)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import tyrex_pm
from tyrex_pm.application import cli as cli_mod

ROOT = Path(tyrex_pm.__file__).resolve().parents[2]
DOCS_LATEST = ROOT / "Docs" / "latest"
SRC = ROOT / "src" / "tyrex_pm"


def _latest_text() -> str:
    parts: list[str] = []
    for path in sorted(DOCS_LATEST.rglob("*.md")):
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_documented_config_files_exist() -> None:
    required = [
        "config/observe_fixture_r3.json",
        "config/observe_live_r3.json",
        "config/observe_shadow_r5.json",
        "config/observe_shadow_r4.json",
        "config/r7/acknowledgment_policy.json",
        "scripts/r8_readonly_recon.py",
        "scripts/r7f_exit_rehearsal.py",
        ".env.example",
    ]
    missing = [p for p in required if not (ROOT / p).exists()]
    assert missing == []


def test_documented_cli_subcommands_exist() -> None:
    parser = cli_mod.build_parser()
    subs = set(parser._subparsers._group_actions[0].choices.keys())  # noqa: SLF001
    required = {
        "version",
        "observe",
        "shadow",
        "discover-btc-window",
        "live-preflight",
        "r7b-live-once",
        "r7c-recon",
        "r7-ack-regenerate",
    }
    assert required.issubset(subs), required - subs


def test_key_source_paths_referenced_in_latest_exist() -> None:
    paths = [
        "strategies/protocol.py",
        "execution/protocol.py",
        "execution/polymarket/settlement.py",
        "execution/polymarket/auth.py",
        "portfolio/portfolio.py",
        "lifecycle/trade_lifecycle.py",
        "runtime/r7b_live_once.py",
        "runtime/r7_lifecycle_policy.py",
    ]
    missing = [p for p in paths if not (SRC / p).exists()]
    assert missing == []


def test_latest_does_not_recommend_old_imports() -> None:
    text = _latest_text()
    assert "from old" not in text
    assert "import old" not in text
    assert "old/src" not in text


def test_latest_does_not_claim_nautilus_dependency() -> None:
    # Strip simple Markdown emphasis so "**not** a dependency" still matches.
    plain = re.sub(r"[*_`]", "", _latest_text()).lower()
    assert "nautilustrader" in plain
    assert "nautilustrader is not a dependency" in plain
    assert "depends on nautilus" not in plain


def test_no_obvious_secrets_or_full_eth_addresses_in_latest() -> None:
    text = _latest_text()
    # Full Ethereum addresses (0x + 40 hex) should not appear in evergreen docs
    addrs = re.findall(r"0x[a-fA-F0-9]{40}", text)
    assert addrs == [], addrs
    # Common secret-looking assignments
    assert "POLYMARKET_API_SECRET=" not in text
    assert "TYREX_PRIVATE_KEY=0x" not in text


def test_strategy_protocol_has_no_timer_or_exec_callbacks() -> None:
    path = SRC / "strategies" / "protocol.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    methods = {
        n.name
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "Strategy"
        for n in n.body
        if isinstance(n, ast.FunctionDef)
    }
    assert "on_start" in methods and "on_signal" in methods and "on_stop" in methods
    assert "on_timer" not in methods
    assert "on_execution_event" not in methods


def test_flat_classification_inventory_states() -> None:
    from tyrex_pm.execution.polymarket.settlement import FlatClassification

    names = {m.name for m in FlatClassification}
    assert {"FLAT", "FLAT_WITH_DUST", "RESIDUAL_EXPOSURE", "UNKNOWN"} <= names
    # External action is not an inventory FlatClassification member
    assert "FLAT_EXTERNAL_ACTION" not in names
