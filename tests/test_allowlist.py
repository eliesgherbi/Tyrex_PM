"""Allowlist YAML loading (v1.01)."""

from pathlib import Path

import pytest
import yaml

from tyrex_pm.data.allowlist import load_market_allowlist


def test_load_default_config():
    root = Path(__file__).resolve().parent.parent
    cfg = root / "config" / "v1_markets.yaml"
    rows = load_market_allowlist(cfg)
    assert len(rows) >= 1
    assert all(r.slug for r in rows)


def test_reject_more_than_five(tmp_path: Path):
    p = tmp_path / "m.yaml"
    markets = [{"slug": f"m{i}"} for i in range(6)]
    p.write_text(yaml.safe_dump({"markets": markets}), encoding="utf-8")
    with pytest.raises(ValueError, match="at most 5"):
        load_market_allowlist(p)


def test_reject_missing_slug(tmp_path: Path):
    p = tmp_path / "m.yaml"
    p.write_text("markets:\n  - note: x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="slug"):
        load_market_allowlist(p)
