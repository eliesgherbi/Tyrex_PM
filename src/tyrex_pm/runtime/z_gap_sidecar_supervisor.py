"""Chainlink sidecar supervision for Z-Gap single-session orchestrator."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tyrex_pm.ingestion.price_to_beat_tracker import DEFAULT_CHAINLINK_TICKS_PATH


@dataclass
class SidecarHealth:
    healthy: bool
    process_running: bool
    tick_age_s: float | None
    last_price: str | None
    details: dict[str, Any]


@dataclass
class SidecarSupervisor:
    """Manage chainlink_tick_logger as a supervised child process."""

    ticks_path: Path = DEFAULT_CHAINLINK_TICKS_PATH
    max_tick_age_s: float = 30.0
    _proc: subprocess.Popen[str] | None = None
    _owned: bool = False

    def _parse_last_tick(self) -> tuple[float | None, str | None]:
        if not self.ticks_path.is_file():
            return None, None
        last_line = ""
        with self.ticks_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last_line = line.strip()
        if not last_line:
            return None, None
        try:
            row = json.loads(last_line)
        except json.JSONDecodeError:
            return None, None
        source_raw = row.get("source_ts")
        price = str(row.get("price") or "") or None
        if not source_raw:
            return None, price
        try:
            dt = datetime.fromisoformat(str(source_raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp(), price
        except ValueError:
            return None, price

    def health(self, *, now_ts: float | None = None) -> SidecarHealth:
        now = now_ts if now_ts is not None else time.time()
        running = self._proc is not None and self._proc.poll() is None
        tick_ts, price = self._parse_last_tick()
        tick_age = (now - tick_ts) if tick_ts is not None else None
        healthy = running and tick_age is not None and tick_age <= self.max_tick_age_s
        return SidecarHealth(
            healthy=healthy,
            process_running=running,
            tick_age_s=tick_age,
            last_price=price,
            details={"ticks_path": str(self.ticks_path), "owned": self._owned},
        )

    def start(self, *, repo_root: Path | None = None) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._owned = False
            return
        root = repo_root or Path.cwd()
        script = root / "scripts" / "chainlink_tick_logger.py"
        if not script.is_file():
            raise FileNotFoundError(f"chainlink sidecar script missing: {script}")
        self.ticks_path.parent.mkdir(parents=True, exist_ok=True)
        self._proc = subprocess.Popen(
            [sys.executable, str(script), "--output", str(self.ticks_path)],
            cwd=str(root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._owned = True

    def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=2)
        self._proc = None
        self._owned = False

    def wait_healthy(
        self,
        *,
        timeout_s: float = 30.0,
        poll_s: float = 0.5,
        now_fn: Any = None,
    ) -> SidecarHealth:
        deadline = (now_fn() if now_fn else time.time()) + timeout_s
        last = self.health(now_ts=now_fn() if now_fn else None)
        while (now_fn() if now_fn else time.time()) < deadline:
            last = self.health(now_ts=now_fn() if now_fn else None)
            if last.healthy:
                return last
            time.sleep(poll_s)
        return last

    def __enter__(self) -> SidecarSupervisor:
        return self

    def __exit__(self, *args: object) -> None:
        if self._owned:
            self.stop()
