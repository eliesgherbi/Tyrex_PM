"""Process-start identity for normal and emergency run reports."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import sys
from decimal import Decimal
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping

from tyrex_pm import __version__
from tyrex_pm.runtime.run_config import effective_entry_cutoff_s, strategy_entry_cutoff_s

_DEPENDENCIES = ("polymarket-client", "PyYAML", "websockets")


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {key: _jsonable(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def _dependency_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in _DEPENDENCIES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _git_identity() -> tuple[str | None, bool | None, str | None]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, None, str(exc)
    if revision.returncode != 0:
        message = revision.stderr.strip() or revision.stdout.strip() or "git rev-parse failed"
        return None, None, message
    try:
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return revision.stdout.strip() or None, None, str(exc)
    if dirty.returncode != 0:
        return revision.stdout.strip() or None, None, dirty.stderr.strip() or "git status failed"
    return revision.stdout.strip() or None, bool(dirty.stdout.strip()), None


def effective_config_payload(config: Any) -> dict[str, Any]:
    hard_s = effective_entry_cutoff_s(config)
    margin_s = config.market.entry_dispatch_margin_s
    return {
        "schema_version": config.schema_version,
        "run_name": config.run_name,
        "strategy_kind": config.strategy_kind,
        "strategy": {
            "kind": config.strategy_kind,
            "parameters": _jsonable(config.strategy),
        },
        "protection": None if config.protection is None else _jsonable(config.protection),
        "market": _jsonable(config.market),
        "entry_cutoffs": {
            "hard_s": hard_s,
            "dispatch_margin_s": margin_s,
            "soft_s": strategy_entry_cutoff_s(config),
        },
        "account": _jsonable(config.account),
        "risk": _jsonable(config.risk),
        "lifecycle": _jsonable(config.lifecycle),
        "state_directory": str(config.state_directory),
        "report_directory": str(config.report_directory),
    }


def capture_reproducibility(
    *,
    config: Any,
    config_path: Path | str | None,
    cli_flags: Mapping[str, Any],
    run_instance_id: str,
    recovery: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    path = None if config_path is None else Path(config_path)
    source_bytes = None if path is None or not path.exists() else path.read_bytes()
    effective = effective_config_payload(config)
    revision, dirty, git_error = _git_identity()
    payload = {
        "package_version": __version__,
        "python_version": sys.version,
        "dependencies": _dependency_versions(),
        "git_revision": revision,
        "git_dirty": dirty,
        "git_revision_error": git_error,
        "config_path": None if path is None else str(path),
        "source_config_sha256": None if source_bytes is None else sha256_text(source_bytes),
        "effective_config": effective,
        "effective_config_sha256": sha256_text(canonical_json(effective)),
        "cli_flags": {
            "live": bool(cli_flags.get("live")),
            "out_dir": None if cli_flags.get("out_dir") is None else str(cli_flags.get("out_dir")),
        },
        "run_instance_id": run_instance_id,
        "session_id": None if recovery is None else recovery.get("session_id"),
        "market_id": None if recovery is None else recovery.get("market_id"),
        "window_id": None if recovery is None else recovery.get("window_id"),
        "token_id": None if recovery is None else recovery.get("token_id"),
        "host_action": None if recovery is None else recovery.get("host_action"),
    }
    return payload
