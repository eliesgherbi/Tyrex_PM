"""Exclusive lock for one execution state directory.

Exclusion is the operating-system lock. A stale PID file is not authority.
Releasing the lock does not delete the journal.
"""

from __future__ import annotations

import os
from pathlib import Path


class StateDirectoryBusy(RuntimeError):
    pass


class StateDirectoryLock:
    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.path = self.directory / ".tyrex_state.lock"
        self._handle: object | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")  # noqa: SIM115 - held until release
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b" ")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise StateDirectoryBusy(
                f"state directory is already locked: {self.directory}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)  # type: ignore[attr-defined]
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        finally:
            handle.close()  # type: ignore[attr-defined]
