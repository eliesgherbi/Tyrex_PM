from __future__ import annotations

from tyrex_pm.core import reason_codes as rc


def check_kill_switch(*, enabled: bool) -> tuple[bool, str | None]:
    if enabled:
        return False, rc.KILL_SWITCH
    return True, None
