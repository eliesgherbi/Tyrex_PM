"""OS ownership protects one ledger even across unrelated execution directories."""
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tyrex_pm.execution.process_lock import LedgerWriter, StateDirectoryBusy
from tyrex_pm.execution.settlement.ledger import LedgerError, SettlementLedger


def test_writer_excludes_other_process_and_releases(tmp_path):
    path = tmp_path / "ledger.jsonl"
    code = (
        "import sys; from tyrex_pm.execution.process_lock import LedgerWriter; "
        "w=LedgerWriter(sys.argv[1]); w.acquire(); print('owned',flush=True); "
        "sys.stdin.readline(); w.release()"
    )
    proc = subprocess.Popen([sys.executable, "-c", code, str(path)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "owned"
        with pytest.raises(StateDirectoryBusy):
            LedgerWriter(path.parent / "." / path.name).acquire()
    finally:
        proc.communicate("release\n", timeout=10)
    with LedgerWriter(path) as writer:
        assert writer.ledger() is writer.ledger()


def test_stale_projection_cannot_write_after_release(tmp_path):
    with LedgerWriter(tmp_path / "l.jsonl") as writer:
        ledger = writer.ledger()
    with pytest.raises(StateDirectoryBusy):
        ledger.record("LOSS_PAUSE_ACKNOWLEDGED", "__operator__", reason="test")


def test_read_only_snapshot_does_not_repair_tail_and_writer_backs_it_up(tmp_path):
    path = tmp_path / "l.jsonl"
    ledger = SettlementLedger(path)
    ledger.record("LOSS_PAUSE_ACKNOWLEDGED", "__operator__", key="one", reason="first")
    with path.open("ab") as f:
        f.write(b'{"unfinished":')
    before = path.read_bytes()
    snapshot = SettlementLedger(path, read_only=True)
    assert snapshot._seq == 1 and snapshot.torn_tail_skipped
    with pytest.raises(LedgerError, match="read-only"):
        snapshot.record("LOSS_PAUSE_ACKNOWLEDGED", "__operator__")
    assert path.read_bytes() == before
    with LedgerWriter(path) as writer:
        writer.ledger().record("LOSS_PAUSE_ACKNOWLEDGED", "__operator__",
                               key="two", reason="second")
    assert next(tmp_path.glob("*.bak")).read_bytes() == before
    assert SettlementLedger(path, read_only=True)._seq == 2


def test_parseable_tail_without_a_terminator_is_not_a_committed_record(tmp_path):
    path = tmp_path / "l.jsonl"
    ledger = SettlementLedger(path)
    ledger.record("LOSS_PAUSE_ACKNOWLEDGED", "__operator__", key="one")
    ledger.record("LOSS_PAUSE_ACKNOWLEDGED", "__operator__", key="two")
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    before = path.read_bytes()
    assert SettlementLedger(path, read_only=True)._seq == 1
    with LedgerWriter(path) as writer:
        assert writer.ledger()._seq == 1
        writer.ledger().record("LOSS_PAUSE_ACKNOWLEDGED", "__operator__", key="replacement")
    assert next(tmp_path.glob("*.bak")).read_bytes() == before
    restored = SettlementLedger(path, read_only=True)
    assert restored._seq == 2
    assert "replacement" in restored._keys and "two" not in restored._keys


def test_compatibility_writers_reload_under_ownership(tmp_path):
    a = SettlementLedger(tmp_path / "l.jsonl")
    b = SettlementLedger(a.path)
    a.record("LOSS_PAUSE_ACKNOWLEDGED", "__operator__", key="one")
    b.record("LOSS_PAUSE_ACKNOWLEDGED", "__operator__", key="two")
    assert b._seq == 2
    with LedgerWriter(a.path):
        with pytest.raises(StateDirectoryBusy):
            a.record("LOSS_PAUSE_ACKNOWLEDGED", "__operator__", key="three")


@pytest.mark.parametrize("command", ["reconcile", "confirm-redemption", "mark-manual",
                                    "ack-loss-pause", "discover-redemptions"])
def test_manual_mutations_refuse_before_network_or_command_work(tmp_path, monkeypatch, command):
    from tyrex_pm.application import settlement_cli

    path = tmp_path / "shared.jsonl"
    config = SimpleNamespace(settlement=SimpleNamespace(ledger_path=path))
    monkeypatch.setattr(settlement_cli, "load_trading_run_config", lambda _: config)

    def unexpected(*args):
        pytest.fail("a competing writer must be refused before command/network work")

    monkeypatch.setattr(settlement_cli, "_run_command", unexpected)
    with LedgerWriter(path):
        assert settlement_cli.run(SimpleNamespace(config="unused", settlement_command=command)) == 3
    assert not path.exists()


@pytest.mark.parametrize("command", ["status", "worklist"])
def test_read_only_commands_work_while_another_writer_owns_the_ledger(
        tmp_path, monkeypatch, command):
    from tyrex_pm.application import settlement_cli

    path = tmp_path / "shared.jsonl"
    config = SimpleNamespace(settlement=SimpleNamespace(ledger_path=path))
    monkeypatch.setattr(settlement_cli, "load_trading_run_config", lambda _: config)

    def read_snapshot(config, args, ledger):
        assert ledger.read_only
        assert ledger._seq == 1
        return 0

    monkeypatch.setattr(settlement_cli, "_run_command", read_snapshot)
    with LedgerWriter(path) as writer:
        writer.ledger().record("LOSS_PAUSE_ACKNOWLEDGED", "__operator__", key="one")
        assert settlement_cli.run(SimpleNamespace(config="unused", settlement_command=command)) == 0
