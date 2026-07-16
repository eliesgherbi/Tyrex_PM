from __future__ import annotations

import json
from pathlib import Path

from tyrex_pm.venue.polymarket.data_api_client import DataApiClient


def test_parse_activity_fixture() -> None:
    raw = Path(__file__).parent / "fixtures" / "data_api" / "activity_batch.json"
    text = raw.read_text(encoding="utf-8")
    sigs = DataApiClient.parse_activity_json(text, "0xguru")
    assert len(sigs) == 3
    assert sigs[0].dedup_key == "act-a"
    assert str(sigs[0].token_id) == "1234567890"
