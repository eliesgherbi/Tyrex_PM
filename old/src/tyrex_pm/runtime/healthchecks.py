from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HeartbeatStatus:
    ok: bool
    last_error: str | None = None
