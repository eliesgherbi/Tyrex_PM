"""``tyrex-pm watch-run`` — read-only live view of a running (or finished) trading run.

Reads only: the run-evidence journal (``<state_directory>/execution.sqlite3``, opened
read-only), the settlement ledger and the run folder's ``windows.jsonl``. It never takes
the state lock, never signs and never touches the venue, so it is safe to run in a second
terminal next to ``tyrex-pm run --live``.

    tyrex-pm watch-run --config <run.yaml>            # one snapshot
    tyrex-pm watch-run --config <run.yaml> -r 5       # refresh every 5 seconds (Ctrl-C)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tyrex_pm.runtime.run_config import RunConfigError, load_trading_run_config

RECENT_S = 60.0


def add_parser(commands: Any) -> None:
    watch = commands.add_parser(
        "watch-run", help="Read-only live view of a run (decisions, entries, windows, ledger)"
    )
    watch.add_argument("--config", type=Path, required=True, help="the run's YAML")
    watch.add_argument("-r", "--refresh", type=float, default=None, metavar="SECONDS",
                       help="refresh every SECONDS until Ctrl-C (default: print once)")
    watch.add_argument("--run-id", default=None, help="run instance id (default: latest)")


def _connect(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2.0)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ago(at: datetime | None, now: datetime) -> str:
    if at is None:
        return "?"
    seconds = int((now - at).total_seconds())
    return f"{seconds}s ago" if seconds < 120 else f"{seconds // 60}m{seconds % 60:02d}s ago"


def snapshot(config_path: Path, run_id: str | None = None) -> str:
    """The text of one view (pure read; used by the CLI and tests)."""
    config = load_trading_run_config(config_path)
    now = datetime.now(timezone.utc)
    lines = [f"tyrex-pm watch-run · {config.run_name} · {now:%Y-%m-%d %H:%M:%S} UTC"]
    con = _connect(config.state_directory / "execution.sqlite3")
    if con is None:
        return "\n".join(lines + ["no run journal yet (has the run started?)"])
    try:
        if run_id:
            row = con.execute("SELECT run_instance_id FROM run_events WHERE "
                              "run_instance_id LIKE ? LIMIT 1", (f"{run_id}%",)).fetchone()
        else:
            # Latest trading run (an account check is not a run); else the latest events.
            row = con.execute(
                "SELECT run_instance_id FROM run_events WHERE event_type='RUN_STARTED' "
                "AND payload_json LIKE '%\"live_requested\":true%' "
                "ORDER BY sequence DESC LIMIT 1").fetchone() or con.execute(
                "SELECT run_instance_id FROM run_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return "\n".join(lines + ["no run events found"])
        rid = row[0]
        recent_runs = con.execute(
            "SELECT run_instance_id, MIN(observed_at), COUNT(*), "
            "SUM(event_type='RUN_FINISHED') FROM run_events GROUP BY run_instance_id "
            "ORDER BY MIN(sequence) DESC LIMIT 4").fetchall()
        events = con.execute(
            "SELECT event_type, observed_at, payload_json FROM run_events "
            "WHERE run_instance_id=? ORDER BY sequence", (rid,)).fetchall()
    finally:
        con.close()

    decisions: Counter = Counter()
    recent: Counter = Counter()
    other: Counter = Counter()
    first_at = last_at = None
    started: dict = {}
    last_decision: dict = {}
    readiness: list[str] = []
    entries: list[dict] = []
    exits: list[dict] = []
    finished = None
    notes: list[str] = []
    for event_type, observed_at, payload_json in events:
        at = _parse_time(observed_at)
        first_at = first_at or at
        last_at = at or last_at
        payload = json.loads(payload_json)
        if event_type == "STRATEGY_DECISION":
            key = f"{payload.get('action')} / {payload.get('reason_code')}"
            decisions[key] += 1
            if at is not None and (now - at).total_seconds() <= RECENT_S:
                recent[key] += 1
            last_decision = payload
        elif event_type == "RUN_STARTED":
            started = payload
        elif event_type == "READINESS_CHANGED":
            readiness = list(payload.get("blockers") or [])
        elif event_type == "ENTRY_OUTCOME":
            entries.append({**payload, "at": observed_at})
        elif event_type == "EXIT_OUTCOME":
            exits.append({**payload, "at": observed_at})
        elif event_type == "RUN_FINISHED":
            finished = payload
        else:
            other[event_type] += 1
            if event_type in ("SETTLEMENT_WORKER_UNAVAILABLE", "LATE_FILL_DETECTED",
                              "RUNTIME_ERROR", "ENTRIES_PAUSED_FOR_WINDOW"):
                notes.append(f"{observed_at[11:19]} {event_type} "
                             f"{json.dumps(payload, default=str)[:160]}")

    status = "FINISHED" if finished is not None else "running"
    if finished is None and last_at is not None and (now - last_at).total_seconds() > 30:
        status = "no events for >30 s (stopped or stalled?)"
    lines += [
        f"run {rid} · {started.get('venue', 'live')} · {started.get('run_mode', '?')} · "
        f"{started.get('lifecycle_mode', '?')}",
        f"status: {status} · started {_ago(first_at, now)} · last event {_ago(last_at, now)}",
        f"readiness blockers: {', '.join(readiness) if readiness else 'none'}",
    ]
    if last_decision:
        evidence = last_decision.get("decision_evidence", {}).get("decision_row", {})
        tau = evidence.get("tau_s")
        lines.append(
            f"last decision: {last_decision.get('action')} / {last_decision.get('reason_code')}"
            f" · window {last_decision.get('window_id')}"
            + (f" · tau {float(tau):.0f}s" if tau is not None else "")
            + (f" · p_up {float(evidence['p_up_point']):.3f}"
               if evidence.get("p_up_point") is not None else "")
            + (f" · up ask {evidence.get('up_ask')} / down ask {evidence.get('down_ask')}"
               if evidence.get("up_ask") else "")
        )
    lines.append("")
    lines.append(f"decisions (total {sum(decisions.values())}; last {RECENT_S:.0f}s in brackets):")
    for key, count in decisions.most_common(10):
        lines.append(f"  {count:6d}  [{recent.get(key, 0):3d}]  {key}")
    lines.append("")
    lines.append(f"entries: {len(entries)}")
    for e in entries[-6:]:
        lines.append(f"  {e['at'][11:19]} {e.get('window_id')} e{e.get('episode_seq')}"
                     f"a{e.get('attempt_seq')} {e.get('kind')} {e.get('reason') or ''} "
                     f"shares={e.get('confirmed_shares')}")
    if exits:
        lines.append(f"exits: {len(exits)}")
        for e in exits[-4:]:
            lines.append(f"  {e['at'][11:19]} {e.get('reason_code')} {e.get('kind')} "
                         f"sold={e.get('sold_shares')}")

    report_root = config.report_directory
    windows_file = None
    if report_root.exists():
        runs = sorted((p for p in report_root.iterdir() if p.is_dir()
                       and (p / "windows.jsonl").exists()),
                      key=lambda p: p.stat().st_mtime)
        windows_file = runs[-1] / "windows.jsonl" if runs else None
    lines.append("")
    if windows_file is not None:
        rows = [json.loads(x) for x in windows_file.read_text(encoding="utf-8").splitlines()
                if x.strip()]
        lines.append(f"windows completed: {len(rows)} ({windows_file.parent.name})")
        for w in rows[-6:]:
            lines.append(f"  {w.get('window_id')}  episodes={w.get('episodes')} "
                         f"attempts={w.get('attempts')} posts={w.get('posts')} "
                         f"states={w.get('states')} phase={w.get('strategy_phase')}")
    else:
        lines.append("windows completed: 0")

    if config.settlement is not None and config.settlement.ledger_path.exists():
        from tyrex_pm.execution.settlement.ledger import SettlementLedger

        ledger = SettlementLedger(config.settlement.ledger_path, fsync=False)
        totals = ledger.totals()
        lines += [
            "",
            "ledger: "
            f"open={totals.open_count} reserved={totals.reserved_cash} "
            f"unresolved_cost={totals.spent_unresolved} "
            f"unredeemed={totals.unredeemed_count} ({totals.unredeemed_cost}) "
            f"realized_pnl={totals.realized_pnl} "
            f"losses_today_utc={ledger.realized_losses_on(now.date())}",
        ]
    if notes:
        lines += ["", "notices:"] + [f"  {n}" for n in notes[-5:]]
    lines += ["", "recent runs (id · start UTC · events · state):"]
    for other_id, start, count, done in recent_runs:
        mark = "  <- shown" if other_id == rid else ""
        lines.append(f"  {other_id[:8]} · {str(start)[11:19]} · {count} · "
                     f"{'finished' if done else 'open'}{mark}")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    try:
        load_trading_run_config(args.config)
    except (RunConfigError, OSError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if args.refresh is None:
        print(snapshot(args.config, args.run_id))
        return 0
    if args.refresh <= 0:
        print("--refresh must be positive", file=sys.stderr)
        return 2
    try:
        while True:
            text = snapshot(args.config, args.run_id)
            footer = f"\n\n(refresh {args.refresh:g}s · Ctrl-C to quit)\n"
            sys.stdout.write("\x1b[2J\x1b[H" + text + footer)
            sys.stdout.flush()
            time.sleep(args.refresh)
    except KeyboardInterrupt:
        return 0
