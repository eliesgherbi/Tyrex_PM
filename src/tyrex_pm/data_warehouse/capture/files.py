"""Same-volume publication, safe manifest paths, and OS-backed ownership."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from ..contracts import canonical


def within(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if Path(relative).is_absolute() or not candidate.is_relative_to(root.resolve()):
        raise ValueError("manifest path escapes storage root")
    return candidate


def replace(source: Path, target: Path, *, attempts: int = 40, delay_s: float = 0.025) -> None:
    """os.replace, retrying Windows sharing violations while a reader holds the target.

    Readers of status/manifest files open them briefly; on Windows that makes an
    atomic rename onto the file fail with PermissionError. The rename is still atomic.
    """
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt" or attempt == attempts - 1:
                raise
            time.sleep(delay_s)


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        replace(temp, path)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(path, (canonical(value) + "\n").encode())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class Ownership:
    def __init__(self, root: Path, role: str):
        root.mkdir(parents=True, exist_ok=True)
        self.handle = (root / f".{role}.lock").open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if not self.handle.tell():
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise ValueError(f"{role} already owns this storage root") from exc

    def close(self) -> None:
        if self.handle.closed:
            return
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()

    def __enter__(self) -> Ownership:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
