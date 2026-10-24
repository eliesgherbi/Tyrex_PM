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
    def __init__(self, directory: Path | str, *, filename: str = ".tyrex_state.lock") -> None:
        self.directory = Path(directory).resolve()
        self.path = self.directory / filename
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

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


class LedgerWriter(StateDirectoryLock):
    """Lifetime ownership of one canonical local ledger, independent of run config.

    A writer has one mutable projection. Acquire execution-directory ownership first
    when both locks are needed. Lock files are never deleted on release.
    """

    def __init__(self, path: Path | str) -> None:
        self.ledger_path = Path(path).resolve()
        if str(self.ledger_path).startswith("\\\\"):
            raise StateDirectoryBusy("ledger ownership requires a local filesystem")
        if self.ledger_path.exists() and self.ledger_path.stat().st_nlink > 1:
            raise StateDirectoryBusy("hard-linked ledgers are unsupported; use one canonical path")
        super().__init__(self.ledger_path.parent, filename=self.ledger_path.name + ".lock")
        self._ledger = None

    def ledger(self):
        if self._handle is None:
            raise StateDirectoryBusy("ledger writer does not own the lock")
        if self._ledger is None:
            from tyrex_pm.execution.settlement.ledger import SettlementLedger

            self._ledger = SettlementLedger(self.ledger_path, writer=self)
        return self._ledger

    def assert_owner(self, ledger) -> None:
        if self._handle is None or self._ledger is not ledger:
            raise StateDirectoryBusy("ledger ownership expired or projection is not authoritative")

    def release(self) -> None:
        self._ledger = None
        super().release()
