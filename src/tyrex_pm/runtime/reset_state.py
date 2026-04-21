"""Clean first-V2-start reset posture (V2 migration §6).

Removes local on-disk state that a future ``tyrex-pm run`` would consume so
that no V1-era artifacts can seed the first V2 process. Reporting artifacts
under ``var/reporting/`` are deliberately preserved (immutable history).

The CLI wrapper in ``tyrex_pm.runtime.app`` exposes this as ``tyrex-pm reset-state``.
The function is idempotent: running it on a clean tree returns an empty list.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


#: Files under ``state_dir`` that carry per-run mutable state which a future
#: ``tyrex-pm run`` would consume. Add any new on-disk state files here.
_RESETTABLE_FILES: tuple[str, ...] = ("guru_strategy_store.json",)


def reset_local_state(state_dir: Path) -> list[Path]:
    """Delete documented local state files under ``state_dir``.

    Returns the list of paths that were actually removed (in input order).
    Idempotent: missing files are silently skipped. Never touches any
    directory other than ``state_dir`` itself, and never recurses.

    Reporting artifacts under ``var/reporting/runs/`` are intentionally
    *not* cleared — they are immutable history, not state.
    """
    removed: list[Path] = []
    if not state_dir.exists():
        return removed
    if not state_dir.is_dir():
        raise NotADirectoryError(f"state_dir is not a directory: {state_dir}")
    for name in _RESETTABLE_FILES:
        p = state_dir / name
        if p.is_file():
            p.unlink()
            removed.append(p)
    return removed


def resettable_file_names() -> tuple[str, ...]:
    """Public accessor for the list of files ``reset_local_state`` will remove."""
    return _RESETTABLE_FILES
