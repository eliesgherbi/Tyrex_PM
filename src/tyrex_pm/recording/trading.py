"""Small decision-time execution inputs; offline export never opens a writable DB."""
import json
import sqlite3
from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from tyrex_pm.core.ids import InstrumentId, RunId
from tyrex_pm.recording.raw import _jsonable
from tyrex_pm.strategies.context import StrategyLifecycleSnapshot, StrategyPositionPhase

CONTEXT_FIELDS = ("position_quantity", "position_cost_total", "entry_allowed",
                  "entry_block_reason", "exit_allowed", "exit_block_reason",
                  "unknown_inventory", "kill_switch_active", "last_exit_reason")


def context_input(context):
    data = {k: _jsonable(getattr(context, k)) for k in CONTEXT_FIELDS}
    data["run_id"] = context.run_id.value
    data["monotonic_ns"] = context.monotonic_ns
    data["observed_at"] = context.snapshot.observed_at.isoformat()
    data["lifecycle"] = None if context.lifecycle is None else {
        "phase": context.lifecycle.phase.value,
        "instrument_id": (None if context.lifecycle.instrument_id is None
                          else context.lifecycle.instrument_id.value),
        "resolution_committed": context.lifecycle.resolution_committed}
    return data


def apply_context(base, data):
    values = {k: data[k] for k in CONTEXT_FIELDS}
    for key in ("position_quantity", "position_cost_total"):
        values[key] = Decimal(values[key])
    life = data["lifecycle"]
    values["lifecycle"] = None if life is None else StrategyLifecycleSnapshot(
        StrategyPositionPhase(life["phase"]),
        None if life["instrument_id"] is None else InstrumentId(life["instrument_id"]),
        life["resolution_committed"])
    values["run_id"] = RunId(data["run_id"])
    values["monotonic_ns"] = data.get("monotonic_ns")
    return replace(base, **values)


def restore_evaluation_clock(ready, captured):
    """Use the captured exogenous snapshot clock, never a recorded model output.

    Raw receipt time still orders input application. A later capture callback is not
    the instant the host constructed its immutable strategy snapshot.
    """
    stamp = captured.get("snapshot_observed_at")
    if stamp is None or ready.snapshot is None:
        return ready
    at = datetime.fromisoformat(stamp)
    if at.tzinfo is None or abs((at - ready.snapshot.observed_at).total_seconds()) > 1:
        raise ValueError("captured evaluation clock outside reconstruction bound")
    return replace(ready, snapshot=replace(ready.snapshot, observed_at=at))


def feedback_input(outcome):
    return _jsonable(asdict(outcome))


def restore_feedback(driver, item, intent_ids):
    from tyrex_pm.strategies.outcomes import (
        EntryOutcome,
        EntryOutcomeKind,
        ExitOutcome,
        ExitOutcomeKind,
        RetryClass,
    )
    kind, data = item["kind"], dict(item["payload"])
    if kind == "seed":
        restore = getattr(driver, "restore_persistence_slice", None)
        if restore is None:
            raise ValueError("strategy cannot restore its captured initial state")
        restore(data)
        for name in ("entry_intent_id", "exit_intent_id"):
            if data.get(name):
                intent_ids[data[name]] = data[name]
    elif kind == "hold":
        driver.on_hold_committed()
    elif kind == "usage":
        driver.restore_window_usage(**data)
    else:
        original = data["intent_id"]
        if original not in intent_ids:
            raise ValueError(f"feedback has no preceding captured intent: {original}")
        data["intent_id"] = intent_ids[original]
        data["at"] = datetime.fromisoformat(data["at"])
        if kind == "entry":
            data["kind"] = EntryOutcomeKind(data["kind"])
            data["retry_class"] = RetryClass(data["retry_class"])
            for key in ("confirmed_shares", "cost"):
                data[key] = Decimal(data[key])
            driver.on_entry_outcome(EntryOutcome(**data))
        elif kind == "exit":
            data["kind"] = ExitOutcomeKind(data["kind"])
            for key in ("remaining_shares", "sold_shares", "proceeds"):
                data[key] = Decimal(data[key])
            driver.on_exit_outcome(ExitOutcome(**data))
        else:
            raise ValueError(f"unknown recorded strategy feedback: {kind}")


def snapshot_journal(source: Path, destination: Path):
    """SQLite backup includes committed WAL pages. Source is opened mode=ro."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)


def export_evidence(config, out, run_id):
    import hashlib

    out = Path(out)
    journal = out / "execution_evidence.sqlite3"
    snapshot_journal(config.state_directory / "execution.sqlite3", journal)
    files = [journal]
    if config.settlement is not None and config.settlement.ledger_path.exists():
        path = out / "settlement_evidence.jsonl"
        raw = config.settlement.ledger_path.read_bytes()
        path.write_bytes(raw[:raw.rfind(b"\n") + 1])
        files.append(path)
    hashes = {}
    for path in files:
        with path.open("rb") as handle:
            hashes[path.name] = hashlib.file_digest(handle, "sha256").hexdigest()
    manifest = {"schema": "recorded_execution_v1", "run_id": run_id, "files": hashes}
    (out / "execution_evidence.json").write_text(json.dumps(manifest, indent=2), "utf-8")


def execution_prefixes(path):
    """Index authoritative per-session prefixes by event ID, preserving reducer order."""
    from tyrex_pm.execution.evidence import evidence_from_dict
    from tyrex_pm.execution.reducer import reduce_execution_event
    from tyrex_pm.execution.session_state import ExecutionSessionState
    from tyrex_pm.runtime.trading_runtime import _lifecycle_view

    states, prefixes = {}, {}
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        for (payload,) in db.execute("SELECT payload_json FROM execution_events ORDER BY sequence"):
            event = evidence_from_dict(json.loads(payload))
            state, _ = reduce_execution_event(
                states.get(event.session_id, ExecutionSessionState()), event)
            states[event.session_id] = state
            prefixes[event.event_id] = {
                "session_id": event.session_id,
                "position_quantity": str(state.confirmed_position_shares),
                "position_cost_total": str(sum(
                    (trade.shares * trade.price for trade in state.trades.values()
                     if trade.side == "BUY"), Decimal(0))),
                "token_id": None if state.identity is None else state.identity.token_id,
                "sellable_shares": str(state.sellable_shares),
                "strategy_phase": _lifecycle_view(state).phase.value,
                "phase": state.phase.value}
    return prefixes
