"""Tests for Chainlink sidecar supervision."""

from __future__ import annotations

import json
import time
from pathlib import Path

from tyrex_pm.runtime.z_gap_sidecar_supervisor import SidecarSupervisor


def test_sidecar_health_from_tick_file(tmp_path: Path) -> None:
    ticks = tmp_path / "ticks.jsonl"
    now = time.time()
    row = {
        "source_ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
        "recv_ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
        "price": "100000.00",
    }
    ticks.write_text(json.dumps(row) + "\n", encoding="utf-8")
    sup = SidecarSupervisor(ticks_path=ticks)
    health = sup.health(now_ts=now)
    assert health.last_price == "100000.00"
    assert health.tick_age_s is not None


def test_sidecar_stop_is_idempotent() -> None:
    sup = SidecarSupervisor()
    sup.stop()
    sup.stop()
