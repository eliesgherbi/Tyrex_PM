"""Run directory + manifest (REC-01)."""

from __future__ import annotations

import json
import logging
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tyrex_pm.reporting.schema.data_quality import RunDataQuality
from tyrex_pm.reporting.sinks.jsonl import JsonlFactSink

_LOG = logging.getLogger(__name__)


def _iso_utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _try_git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()[:40]
    except (OSError, subprocess.CalledProcessError):
        return None


@dataclass
class RunContext:
    run_id: str
    run_dir: Path
    strategy_name: str
    trader_id: str
    sink: JsonlFactSink
    facts_path: Path
    manifest_path: Path
    tyrex_log_path: str | None = None
    nautilus_log_path: str | None = None
    data_quality: RunDataQuality = field(default_factory=RunDataQuality)
    execution_path: str = "unknown"  # live | shadow (aligned with execution_mode)

    def emit(self, fact_type: str, payload: dict[str, Any]) -> None:
        self.sink.emit_fact(fact_type, payload)

    def write_manifest_partial(
        self,
        *,
        started_at_utc: str,
        git_sha: str | None,
        host: str,
    ) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        man = {
            "run_id": self.run_id,
            "started_at_utc": started_at_utc,
            "ended_at_utc": None,
            "strategy_name": self.strategy_name,
            "trader_id": self.trader_id,
            "run_dir": str(self.run_dir.resolve()),
            "facts_path": str(self.facts_path.resolve()),
            "git_sha": git_sha,
            "host": host,
            "tyrex_log_path": self.tyrex_log_path,
            "nautilus_log_path": self.nautilus_log_path,
            "execution_path": self.execution_path,
            "data_quality": self.data_quality.to_dict(),
        }
        self.manifest_path.write_text(json.dumps(man, indent=2) + "\n", encoding="utf-8")

    def update_manifest_fields(self, **fields: Any) -> None:
        """Merge non-None fields into ``manifest.json`` (e.g. ``execution_path`` after compose)."""
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        man: dict[str, Any]
        if self.manifest_path.is_file():
            man = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        else:
            man = {"run_id": self.run_id}
        for k, v in fields.items():
            if v is not None:
                man[k] = v
        if "data_quality" not in man:
            man["data_quality"] = self.data_quality.to_dict()
        self.manifest_path.write_text(json.dumps(man, indent=2) + "\n", encoding="utf-8")

    def finalize_manifest(self, *, run_ended_cleanly: bool) -> None:
        self.data_quality.run_ended_cleanly = run_ended_cleanly
        if not run_ended_cleanly:
            self.data_quality.facts_incomplete = True
        stats = self.sink.drain_and_close()
        try:
            from tyrex_pm.reporting.post_run_dq import apply_fact_file_heuristics

            apply_fact_file_heuristics(self.data_quality, self.facts_path, run_context=self)
        except OSError as exc:
            _LOG.warning("reporting post-run data quality scan failed: %s", exc)
        ended = _iso_utc_now()
        man: dict[str, Any]
        if self.manifest_path.is_file():
            man = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        else:
            man = {"run_id": self.run_id}
        man["ended_at_utc"] = ended
        man["execution_path"] = self.execution_path
        man["data_quality"] = self.data_quality.to_dict()
        man["reporting_sink_stats"] = stats
        self.manifest_path.write_text(json.dumps(man, indent=2) + "\n", encoding="utf-8")


def create_run_context(
    *,
    repo_root: Path,
    run_id: str,
    strategy_name: str,
    trader_id: str,
    reporting_base_dir: str = "var/reporting/runs",
    tyrex_log_path: str | None = None,
    nautilus_log_path: str | None = None,
    sink_max_queue: int = 50_000,
    sink_batch_size: int = 128,
    sink_flush_interval_s: float = 0.05,
) -> RunContext:
    run_dir = (repo_root / reporting_base_dir / run_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    facts_path = run_dir / "facts.jsonl"
    manifest_path = run_dir / "manifest.json"
    sink = JsonlFactSink(
        facts_path,
        run_id=run_id,
        max_queue=sink_max_queue,
        batch_size=sink_batch_size,
        flush_interval_s=sink_flush_interval_s,
    )
    sink.start()
    ctx = RunContext(
        run_id=run_id,
        run_dir=run_dir,
        strategy_name=strategy_name,
        trader_id=trader_id,
        sink=sink,
        facts_path=facts_path,
        manifest_path=manifest_path,
        tyrex_log_path=tyrex_log_path,
        nautilus_log_path=nautilus_log_path,
    )
    started = _iso_utc_now()
    ctx.write_manifest_partial(
        started_at_utc=started,
        git_sha=_try_git_sha(),
        host=socket.gethostname(),
    )
    return ctx
