"""Automatic recognition of UI redemptions: proof, attribution, idempotency, recovery.

The fixture is the real Polymarket UI redemption of window btc-updown-5m-1792187400
(public Polygon tx 0x85983a7f…, block 95654969), trimmed to our operation and the
neighbouring users' operations in the same bundle.
"""

from __future__ import annotations

import asyncio
import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.adapters.polymarket.polygon_rpc import PolygonRpcError
from tyrex_pm.execution.settlement.chain import (
    POLYMARKET_ADAPTER_PUSD,
    receipt_redemption_verifier,
    redemption_matches,
)
from tyrex_pm.execution.settlement.ledger import EpisodeState, SettlementLedger
from tyrex_pm.execution.settlement.redemption_discovery import (
    DiscoveryPolicy,
    LogScanSource,
    RedemptionDiscovery,
)
from tyrex_pm.execution.settlement.resolution import PayoutObservation, ResolutionManager

pytest.importorskip("eth_utils")

S = EpisodeState
FIXTURE = (Path(__file__).resolve().parents[2]
           / "fixtures/settlement/ui_redemption_bundle_95654969.json")
WALLET = "0x64367d66bf08d3c8623530bc3113e60908c82bb0"
COND = "0x16242e12032dcb48b831bccb2872ef23bb143c493c096faae2de6bce917ec49c"
TOKEN = "84382869804484180560606672132469979747174040333575988942842362783760174284001"
BLOCK = 95654969
CREDIT = Decimal("10.25532")


def receipt() -> dict:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return copy.deepcopy(data["receipt"])


def test_the_real_ui_redemption_is_proven_for_our_wallet_only() -> None:
    (match,) = redemption_matches(receipt(), wallet=WALLET)
    assert match.route == POLYMARKET_ADAPTER_PUSD.name
    assert match.condition_id == COND and match.credited == CREDIT
    assert match.positions == {TOKEN: 10_255_320} and match.log_index == 1714
    assert match.credited_asset == POLYMARKET_ADAPTER_PUSD.credited_token
    assert match.covers(COND, 1, TOKEN) and not match.covers(COND, 0, "1")
    # Other users redeemed the same market in this bundle: each operation is proven for
    # its own wallet only, never credited to ours.
    (theirs,) = redemption_matches(receipt(),
                                   wallet="0x63d2233fab3ca4233f7a42c2dbb10b338f25ed0d")
    assert theirs.log_index == 1704 and theirs.credited != CREDIT
    assert redemption_matches(receipt(), wallet="0x" + "99" * 20) == []


@pytest.mark.parametrize("tamper,reason", [
    ("credit", "CREDIT_TRANSFER_MISMATCH"),
    ("surrender", "NO_POSITION_SURRENDERED_BY_WALLET"),
    ("failed", None),
])
def test_every_fact_of_the_route_is_required(tamper, reason) -> None:
    rc = receipt()
    for log in rc["logs"]:
        index = int(log["logIndex"], 16)
        if tamper == "credit" and index == 1711:  # pUSD mint to our wallet
            log["data"] = "0x" + hex(10_255_321)[2:].rjust(64, "0")
        if tamper == "surrender" and index == 1705:  # tokens leave our wallet
            log["topics"][2] = "0x" + "0" * 24 + "22" * 20
    if tamper == "failed":
        rc["status"] = "0x0"
    rejections: list[dict] = []
    assert redemption_matches(rc, wallet=WALLET, rejections=rejections) == []
    if reason:
        assert any(reason in r["reason"] for r in rejections)


def test_confirm_redemption_verifier_accepts_the_adapter_route() -> None:
    from tyrex_pm.execution.settlement.ledger import Episode

    class Reader:
        async def receipt(self, tx):
            return receipt()

        async def finalized_block_number(self):
            return BLOCK + 10

    episode = Episode(episode_id="e", window_id="w", market_id="m", condition_id=COND,
                      token_id=TOKEN, outcome_index=1)
    evidence = asyncio.run(receipt_redemption_verifier(Reader())("0xtx", episode, WALLET))
    assert evidence.attributable and evidence.finalized and evidence.credited_amount == CREDIT
    assert evidence.log_index == 1714 and evidence.route == POLYMARKET_ADAPTER_PUSD.name


# -- discovery over a fake read-only chain ----------------------------------------------

class FakeChain:
    def __init__(self, *, finalized=BLOCK + 50, transfers=True, fail=0):
        self.finalized = finalized
        self.transfers = transfers
        self.fail = fail
        self.calls: dict[str, int] = {}

    def _count(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1
        if self.fail:
            self.fail -= 1
            raise PolygonRpcError("polygon_rpc_error")

    async def finalized_block_number(self):
        self._count("finalized")
        return self.finalized

    async def block_timestamp(self, number):
        # The test ledger's fills happen "now": place now 1000 blocks before the redemption.
        import time

        self._count("timestamp")
        return int(time.time()) + (number - (BLOCK - 1000)) * 2

    async def asset_transfers(self, params):
        self._count("transfers")
        if not self.transfers:
            raise PolygonRpcError("polygon_rpc_method")
        lo, hi = int(params["fromBlock"], 16), int(params["toBlock"], 16)
        hits = [{"hash": receipt()["transactionHash"], "blockNum": hex(BLOCK)}]
        return {"transfers": [h for h in hits if lo <= BLOCK <= hi]}

    async def logs(self, *, address, topics, from_block, to_block):
        self._count("logs")
        if from_block <= BLOCK <= to_block:
            return [{"transactionHash": receipt()["transactionHash"], "blockNumber": hex(BLOCK)}]
        return []

    async def receipt(self, tx):
        self._count("receipt")
        return receipt()


def _ledger(tmp_path: Path, shares=("10.25532",), *, prove=True) -> SettlementLedger:
    ledger = SettlementLedger(tmp_path / "ledger.jsonl")
    for n, amount in enumerate(shares, start=1):
        ep = f"ep{n}"
        ledger.record("EPISODE_RESERVED", ep, window_id="w", market_id="m", condition_id=COND,
                      token_id=TOKEN, outcome_index=1, max_debit="5", max_fees="0.1",
                      window_episode_cap=len(shares))
        ledger.record("FILL_CONFIRMED", ep, net_shares=amount, cost_basis="4")
        ledger.record("HOLD_REQUESTED", ep, quantity=amount)
        ledger.record("HOLD_COMMITTED", ep, quantity=amount)
    if prove:
        _prove(ledger)
    return ledger


def _manager(ledger) -> ResolutionManager:
    async def payout(condition, outcome):
        return PayoutObservation(True, Decimal(1), "ctf")

    return ResolutionManager(ledger, payout_reader=payout, wallet=WALLET)


def _prove(ledger):
    asyncio.run(_manager(ledger).reconcile())


def _discovery(ledger, tmp_path, chain=None, **policy) -> RedemptionDiscovery:
    return RedemptionDiscovery(_manager(ledger), chain or FakeChain(), wallet=WALLET,
                               state_path=tmp_path / "redemption_discovery.json",
                               policy=DiscoveryPolicy(**policy))


def test_dry_run_reports_then_apply_credits_once_across_restarts(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    assert ledger.episodes["ep1"].state is S.REDEMPTION_PENDING_OPERATOR
    dry = asyncio.run(_discovery(ledger, tmp_path).run_once(apply=False))
    assert dry["would_apply"][0]["episodes"] == ["ep1"] and not dry["applied"]
    assert ledger.episodes["ep1"].state is S.REDEMPTION_PENDING_OPERATOR

    report = asyncio.run(_discovery(ledger, tmp_path).run_once(apply=True))
    assert [a["episodes"] for a in report["applied"]] == [["ep1"]]
    ep = ledger.episodes["ep1"]
    assert ep.state is S.COMPLETED and ep.credited == CREDIT
    assert ep.evidence[-1]["log_index"] == 1714 and ep.evidence[-1]["route"]
    assert report["totals_before"]["redeemed_cash"] == "0"
    assert Decimal(report["totals_after"]["redeemed_cash"]) == CREDIT

    # Polling again, a restart (fresh ledger + discovery objects) and a manual confirm
    # of the same transaction credit nothing more.
    again = asyncio.run(_discovery(ledger, tmp_path).run_once())
    restarted_ledger = SettlementLedger(tmp_path / "ledger.jsonl")
    restarted = asyncio.run(_discovery(restarted_ledger, tmp_path).run_once())
    assert not again["applied"] and not restarted["applied"]
    assert restarted_ledger.totals().redeemed_cash == CREDIT
    manager = _manager(restarted_ledger)
    assert manager.already_credited(receipt()["transactionHash"], 1714) == "ep1"
    audit = (tmp_path / "redemption_discovery_audit.jsonl").read_text().splitlines()
    assert any('"REDEMPTION_APPLIED"' in line for line in audit)


def test_a_redemption_seen_before_the_payout_is_proven_is_applied_later(tmp_path) -> None:
    ledger = _ledger(tmp_path, prove=False)
    first = asyncio.run(_discovery(ledger, tmp_path).run_once())
    (item,) = first["unresolved"].values()
    assert item["reason"].startswith("HOLDER_NOT_YET_AWAITING_REDEMPTION")
    _prove(ledger)  # the runner's reconcile proves the payout afterwards
    chain = FakeChain()
    second = asyncio.run(_discovery(ledger, tmp_path, chain).run_once())
    assert [a["episodes"] for a in second["applied"]] == [["ep1"]]
    assert "receipt" not in chain.calls  # cursor already past the tx: stored match reused


def test_one_redemption_covering_two_episodes_is_allocated_by_receivable(tmp_path) -> None:
    ledger = _ledger(tmp_path, shares=("5", "5.25532"))
    report = asyncio.run(_discovery(ledger, tmp_path).run_once())
    (applied,) = report["applied"]
    assert sorted(applied["episodes"]) == ["ep1", "ep2"]
    assert ledger.episodes["ep1"].credited == Decimal("5")
    assert ledger.episodes["ep2"].credited == Decimal("5.25532")


@pytest.mark.parametrize("shares,reason", [
    (("10",), "SURRENDERED_SHARES_DIFFER_FROM_LEDGER"),
])
def test_unproven_attribution_stays_unresolved(tmp_path, shares, reason) -> None:
    ledger = _ledger(tmp_path, shares=shares)
    report = asyncio.run(_discovery(ledger, tmp_path).run_once())
    assert not report["applied"]
    (item,) = report["unresolved"].values()
    assert item["reason"].startswith(reason)
    assert ledger.episodes["ep1"].state is S.REDEMPTION_PENDING_OPERATOR


def test_rpc_errors_back_off_and_the_log_scan_fallback_finds_the_candidate(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    failing = _discovery(ledger, tmp_path, FakeChain(fail=1))
    first = asyncio.run(failing.run_once())
    assert "scan_error" in first and not first["applied"]
    assert asyncio.run(failing.run_once())["skipped"].startswith("backing off")
    chain = FakeChain(transfers=False)
    fallback = RedemptionDiscovery(_manager(ledger), chain, wallet=WALLET,
                                   state_path=tmp_path / "redemption_discovery.json",
                                   policy=DiscoveryPolicy(log_chunk_blocks=10,
                                                          max_log_calls_per_pass=10_000))
    report = asyncio.run(fallback.run_once(force=True))
    assert isinstance(fallback._source, LogScanSource) and report["applied"]


def test_manual_confirmation_of_an_already_discovered_redemption_is_refused(tmp_path) -> None:
    ledger = _ledger(tmp_path, shares=("5", "5.25532"))
    asyncio.run(_discovery(ledger, tmp_path).run_once())
    manager = _manager(ledger)
    assert manager.already_credited(receipt()["transactionHash"], 1714) in ("ep1", "ep2")


def test_the_worker_runs_discovery_at_startup(tmp_path) -> None:
    from tyrex_pm.runtime.settlement_worker import SettlementWorker

    ledger = _ledger(tmp_path)
    events: list[tuple] = []

    def record(kind, payload):
        events.append((kind, payload))

    discovery = _discovery(ledger, tmp_path)
    discovery.record = record  # the runtime passes run_recorder.record to both
    worker = SettlementWorker(manager=_manager(ledger), interval_s=30, record=record,
                              discovery=discovery)
    report = asyncio.run(worker.once())
    assert report["redemption_discovery"]["applied"]
    assert [k for k, _ in events] == ["REDEMPTION_DISCOVERED", "SETTLEMENT_RECONCILED"]


def test_settlement_config_block_is_validated(tmp_path) -> None:
    from tyrex_pm.runtime.run_config import RunConfigError, _redemption_discovery

    assert _redemption_discovery(None).enabled
    assert _redemption_discovery({"interval_s": 60}).interval_s == 60
    with pytest.raises(RunConfigError):
        _redemption_discovery({"interval_s": 0})
    with pytest.raises(RunConfigError):
        _redemption_discovery({"bogus": 1})
