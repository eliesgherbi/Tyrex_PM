"""Guru run file logging helpers for ``scripts/run_guru.py``.

Resolves per-source log paths (Tyrex stdlib vs Nautilus-native file sink), sanitizes
``--log-name``, attaches a :class:`logging.FileHandler` to the ``tyrex_pm`` logger
(not root — avoids turning the Tyrex file into an HTTP/third-party catch-all).
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }
)

_LOG_NAME_RE = re.compile(r"^[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*$")

GuruLogSource = Literal["tyrex", "nautilus"]


def sanitize_log_name(name: str | None) -> str:
    """Return safe CLI basename (no ``.log`` / source suffix). Default ``run``."""
    if name is None:
        return "run"
    s = name.strip()
    if not s:
        return "run"
    if len(s) > 100:
        raise ValueError("log name too long (max 100 characters)")
    if "\x00" in s or any(ch in s for ch in "\r\n"):
        raise ValueError("log name contains invalid control characters")
    if "/" in s or "\\" in s or ":" in s:
        raise ValueError("log name must not contain path separators or ':'")
    if ".." in s or s in (".", ".."):
        raise ValueError("log name must not be '.' or '..' or contain '..'")
    if s.startswith("-"):
        raise ValueError("log name must not start with '-'")
    if not _LOG_NAME_RE.fullmatch(s):
        raise ValueError(
            "log name must use only letters, digits, and ._- "
            "(no spaces; dots/hyphens only between segments)"
        )
    stem = s.split(".")[0].upper()
    if stem in _WINDOWS_RESERVED:
        raise ValueError(f"log name reserved on Windows: {stem}")
    return s


def resolve_guru_source_log_path(
    repo_root: Path,
    execution_mode: str,
    log_name_arg: str | None,
    source: GuruLogSource,
) -> Path:
    """Return ``repo_root/logs/<mode>/<base>_<source>.log`` (absolute)."""
    base = sanitize_log_name(log_name_arg)
    mode = (execution_mode or "live").strip().lower()
    if not mode:
        mode = "live"
    filename = f"{base}_{source}.log"
    return (repo_root.resolve() / "logs" / mode / filename).resolve()


@dataclass(frozen=True, slots=True)
class GuruNautilusFileLogging:
    """Arguments for Nautilus :class:`LoggingConfig` file sink (framework-native).

    ``log_file_stem`` must be **without** ``.log``; Nautilus appends the suffix.
    """

    log_directory: str
    log_file_stem: str


def ensure_guru_run_log_dir(log_file: Path) -> None:
    """Create parent directory for the log file."""
    log_file.parent.mkdir(parents=True, exist_ok=True)


def attach_tyrex_pm_file_handler(
    log_file: Path,
    *,
    fmt: str = "%(message)s",
) -> logging.FileHandler | None:
    """Append a file handler to the ``tyrex_pm`` logger (and only that subtree).

    Child loggers such as ``tyrex_pm.runtime.guru_compose`` propagate up through
    intermediate parents to ``tyrex_pm``, so their records reach this handler.
    """
    ensure_guru_run_log_dir(log_file)
    logger = logging.getLogger("tyrex_pm")
    try:
        target = log_file.resolve()
    except OSError:
        target = log_file
    resolved_target = str(target)
    for h in logger.handlers:
        if isinstance(h, logging.FileHandler):
            try:
                existing = Path(h.baseFilename).resolve()
            except OSError:
                existing = Path(h.baseFilename)
            if str(existing) == resolved_target:
                return None
    fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    logger.addHandler(fh)
    return fh


def format_tyrex_logging_destination_line(log_file: Path) -> str:
    try:
        display = log_file.resolve()
    except OSError:
        display = log_file
    return f"tyrex_pm logging to {display}"


def format_nautilus_logging_destination_line(log_file: Path) -> str:
    try:
        display = log_file.resolve()
    except OSError:
        display = log_file
    return f"nautilus logging to {display}"


def announce_guru_run_log_destinations(
    tyrex_log_file: Path,
    nautilus_log_file: Path,
    *,
    use_print: bool = True,
) -> tuple[str, str]:
    """Print operator-visible paths for Tyrex stdlib vs Nautilus file sinks."""
    ty = format_tyrex_logging_destination_line(tyrex_log_file)
    na = format_nautilus_logging_destination_line(nautilus_log_file)
    if use_print:
        print(ty, file=sys.stdout)
        print(na, file=sys.stdout)
    return ty, na
