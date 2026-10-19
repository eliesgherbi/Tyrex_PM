"""Automatic recognition of redemptions made in the Polymarket UI (read-only).

The operator redeems in the venue UI; nothing here signs, redeems or transfers. A
pass of ``RedemptionDiscovery.run_once``:

1. finds *candidate* transactions: collateral credits (USDC.e or pUSD transfers) to our
   wallet in a bounded block range up to the **finalized** block. The candidate source
   is Alchemy's transfer index when available, otherwise ``eth_getLogs`` in small
   adaptive chunks. A candidate is only a pointer, never evidence;
2. proves redemptions from each candidate's receipt with ``chain.redemption_matches``
   (every fact of a route must hold: who surrendered which position tokens, which
   condition was redeemed, how much was credited, to whom);
3. stores every proven match (so a redemption made before the ledger reached
   ``REDEMPTION_PENDING_OPERATOR`` is still applied later) and advances a persistent
   cursor;
4. attributes each unapplied match to ledger episodes. It applies the credit through
   ``ResolutionManager.apply_redemption`` (the existing ``CASH_CONFIRMED`` /
   ``EPISODE_COMPLETED`` transitions) only when attribution is proven: every
   unsettled episode holding the redeemed position awaits redemption, the surrendered
   shares equal the ledger's shares, and the receivables add up to the credit.
   Otherwise the match stays an explicit unresolved item, retried next pass.

A redemption log is credited at most once (ledger evidence carries tx hash and log
index; manual ``confirm-redemption`` uses the same guard). Loss allowances are never
touched. Progress, matches, unresolved items and errors live in
``<ledger dir>/redemption_discovery.json``; every decision is appended to
``redemption_discovery_audit.jsonl``.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from tyrex_pm.adapters.polymarket.polygon_rpc import PolygonRpcError

from .chain import (
    DEFAULT_ADAPTER_ROUTES,
    DEFAULT_COLLATERAL,
    ERC20_TRANSFER_TOPIC,
    AdapterRoute,
    RedemptionMatch,
    redemption_matches,
)
from .ledger import UNRESOLVED_STATES, EpisodeState
from .resolution import ResolutionManager

S = EpisodeState
STATE_SCHEMA = 1
# Unsettled holders of a position (a redemption can only be attributed when all of them
# await redemption). MANUAL_INTERVENTION holders always keep the item unresolved.
HOLDER_STATES = set(UNRESOLVED_STATES) | {S.MANUAL_INTERVENTION}
POLYGON_BLOCK_S = 2.1  # starting estimate only; the start block is found by bisection


@dataclass
class DiscoveryPolicy:
    interval_s: float = 300.0
    max_blocks_per_pass: int = 200_000  # ~4.9 days of Polygon blocks
    max_log_calls_per_pass: int = 400  # eth_getLogs fallback budget per pass
    log_chunk_blocks: int = 10  # starting chunk (Alchemy free tier allows 10)
    start_margin_blocks: int = 300
    backoff_base_s: float = 60.0
    backoff_max_s: float = 3600.0


class TransferIndexSource:
    """Alchemy ``alchemy_getAssetTransfers``: ERC-20 transfers to the wallet."""

    name = "alchemy_transfer_index"

    async def candidates(self, reader: Any, *, wallet: str, assets: list[str],
                         from_block: int, to_block: int, policy: DiscoveryPolicy
                         ) -> tuple[list[tuple[str, int]], int]:
        out: dict[str, int] = {}
        page_key = None
        calls = 0
        while True:
            params = {"fromBlock": hex(from_block), "toBlock": hex(to_block),
                      "toAddress": wallet, "contractAddresses": assets,
                      "category": ["erc20"], "maxCount": "0x3e8", "excludeZeroValue": True}
            if page_key:
                params["pageKey"] = page_key
            result = await reader.asset_transfers(params)
            calls += 1
            for item in result.get("transfers") or []:
                out[str(item["hash"]).lower()] = int(str(item["blockNum"]), 16)
            page_key = result.get("pageKey")
            if not page_key:
                return sorted(out.items(), key=lambda kv: kv[1]), to_block


class LogScanSource:
    """``eth_getLogs`` for collateral ``Transfer(to = wallet)``; adaptive chunks."""

    name = "eth_getLogs"

    def __init__(self) -> None:
        self.chunk = 0

    async def candidates(self, reader: Any, *, wallet: str, assets: list[str],
                         from_block: int, to_block: int, policy: DiscoveryPolicy
                         ) -> tuple[list[tuple[str, int]], int]:
        self.chunk = self.chunk or policy.log_chunk_blocks
        wallet_topic = "0x" + "0" * 24 + wallet.lower()[2:]
        out: dict[str, int] = {}
        start, calls = from_block, 0
        while start <= to_block and calls < policy.max_log_calls_per_pass:
            end = min(to_block, start + self.chunk - 1)
            try:
                logs = await reader.logs(address=assets,
                                         topics=[ERC20_TRANSFER_TOPIC, None, wallet_topic],
                                         from_block=start, to_block=end)
            except PolygonRpcError:
                calls += 1
                if self.chunk == 1:
                    raise
                self.chunk = max(1, self.chunk // 2)
                continue
            calls += 1
            for log in logs:
                out[str(log["transactionHash"]).lower()] = int(str(log["blockNumber"]), 16)
            start = end + 1
        return sorted(out.items(), key=lambda kv: kv[1]), start - 1


class RedemptionDiscovery:
    def __init__(self, manager: ResolutionManager, reader: Any, *, wallet: str,
                 state_path: Path, policy: DiscoveryPolicy | None = None,
                 routes: tuple[AdapterRoute, ...] = DEFAULT_ADAPTER_ROUTES,
                 collateral: str = DEFAULT_COLLATERAL, source: Any = None,
                 record: Callable[[str, dict], None] | None = None) -> None:
        self.manager = manager
        self.reader = reader
        self.wallet = wallet.lower()
        self.state_path = Path(state_path)
        self.audit_path = self.state_path.with_name("redemption_discovery_audit.jsonl")
        self.policy = policy or DiscoveryPolicy()
        self.routes = routes
        self.collateral = collateral
        self.assets = [collateral.lower()] + [r.credited_token.lower() for r in routes]
        self._source = source
        self.record = record or (lambda kind, payload: None)
        self.state = self._load()
        self._last_pass_mono = 0.0

    # -- persistence -------------------------------------------------------------------
    def _load(self) -> dict:
        if self.state_path.exists():
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if data.get("wallet", self.wallet) != self.wallet:
                raise RuntimeError("redemption discovery state belongs to another wallet")
            return data
        return {"schema": STATE_SCHEMA, "wallet": self.wallet, "next_block": None,
                "source": None, "matches": {}, "rejections": [], "unresolved": {},
                "applied": {}, "scanned_transactions": 0,
                "errors": {"consecutive": 0, "retry_after": None, "last": None}}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True, default=str),
                       encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _audit(self, kind: str, payload: dict) -> None:
        line = {"at": datetime.now(timezone.utc).isoformat(), "kind": kind, **payload}
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True, default=str) + "\n")

    # -- pass --------------------------------------------------------------------------
    def due(self, now_mono: float | None = None) -> bool:
        now_mono = time.monotonic() if now_mono is None else now_mono
        return now_mono - self._last_pass_mono >= self.policy.interval_s

    def _backing_off(self) -> bool:
        retry_after = self.state["errors"].get("retry_after")
        return bool(retry_after) and datetime.fromisoformat(retry_after) > datetime.now(
            timezone.utc)

    async def run_once(self, *, apply: bool = True, force: bool = False) -> dict:
        self._last_pass_mono = time.monotonic()
        ledger = self.manager.ledger
        before = ledger.totals()
        report: dict[str, Any] = {"apply": apply, "scanned": None, "new_matches": [],
                                  "applied": [], "unresolved": {}, "skipped": None}
        holders = [ep for ep in ledger.episodes.values() if ep.state in HOLDER_STATES]
        if not holders and not self._unapplied():
            report["skipped"] = "no unsettled episodes"
            return self._finish(report, before)
        if self._backing_off() and not force:
            report["skipped"] = f"backing off until {self.state['errors']['retry_after']}"
            return self._finish(report, before)
        try:
            report["scanned"] = await self._scan(holders)
            self.state["errors"] = {"consecutive": 0, "retry_after": None, "last": None}
        except (PolygonRpcError, OSError, TimeoutError, ValueError, KeyError) as exc:
            errors = self.state["errors"]
            errors["consecutive"] = int(errors.get("consecutive", 0)) + 1
            delay = min(self.policy.backoff_max_s,
                        self.policy.backoff_base_s * 2 ** (errors["consecutive"] - 1))
            errors["retry_after"] = (datetime.now(timezone.utc)
                                     + timedelta(seconds=delay)).isoformat()
            errors["last"] = f"{type(exc).__name__}: {exc}"
            report["scan_error"] = errors["last"]
            self._save()
            self._audit("SCAN_ERROR", {"error": errors["last"], "retry_in_s": delay})
        self._attribute(report, apply=apply)
        return self._finish(report, before)

    def _finish(self, report: dict, before: Any) -> dict:
        after = self.manager.ledger.totals()
        keys = ("redeemed_cash", "winning_receivable", "winning_receivable_count",
                "unredeemed_count", "realized_pnl", "realized_losses")
        report["totals_before"] = {k: str(getattr(before, k)) for k in keys}
        report["totals_after"] = {k: str(getattr(after, k)) for k in keys}
        report["unresolved"] = dict(self.state["unresolved"])
        report["cursor_next_block"] = self.state.get("next_block")
        self._save()
        return report

    # -- scanning ----------------------------------------------------------------------
    async def _start_block(self, holders: list, finalized: int) -> int:
        stamps = [getattr(ep, "filled_at", None) or getattr(ep, "opened_at", None)
                  for ep in holders]
        stamps = [datetime.fromisoformat(s) for s in stamps if s]
        if not stamps:
            return max(0, finalized - self.policy.max_blocks_per_pass)
        target = int(min(stamps).timestamp())
        low = max(0, finalized - int((time.time() - target) / POLYGON_BLOCK_S * 1.5) - 1000)
        high = finalized
        low_ts = await self.reader.block_timestamp(low)
        if low_ts is not None and low_ts > target:
            low = 0
        while high - low > 1:  # last block with timestamp <= target
            mid = (low + high) // 2
            ts = await self.reader.block_timestamp(mid)
            if ts is None:
                raise ValueError("block timestamp unavailable")
            if ts <= target:
                low = mid
            else:
                high = mid
        return max(0, low - self.policy.start_margin_blocks)

    async def _source_for(self) -> Any:
        if self._source is not None:
            return self._source
        try:
            await self.reader.asset_transfers({"fromBlock": "0x0", "toBlock": "0x0",
                                               "toAddress": self.wallet,
                                               "category": ["erc20"], "maxCount": "0x1"})
            self._source = TransferIndexSource()
        except PolygonRpcError:
            self._source = LogScanSource()
        return self._source

    async def _scan(self, holders: list) -> dict | None:
        finalized = await self.reader.finalized_block_number()
        if finalized is None:
            raise ValueError("finalized block unavailable")
        if self.state.get("next_block") is None:
            if not holders:
                return None
            self.state["next_block"] = await self._start_block(holders, finalized)
            self._audit("CURSOR_INITIALISED", {"next_block": self.state["next_block"]})
        start = int(self.state["next_block"])
        if start > finalized:
            return {"from": start, "to": finalized, "candidates": 0}
        end = min(finalized, start + self.policy.max_blocks_per_pass - 1)
        source = await self._source_for()
        self.state["source"] = source.name
        candidates, scanned_to = await source.candidates(
            self.reader, wallet=self.wallet, assets=self.assets, from_block=start,
            to_block=end, policy=self.policy)
        new = 0
        for tx_hash, _block in candidates:
            if any(m["tx_hash"].lower() == tx_hash for m in self.state["matches"].values()):
                continue
            receipt = await self.reader.receipt(tx_hash)
            if receipt is None:
                raise ValueError(f"receipt unavailable for {tx_hash}")
            rejections: list[dict] = []
            for match in redemption_matches(receipt, wallet=self.wallet,
                                            collateral=self.collateral, routes=self.routes,
                                            rejections=rejections):
                if match.block_number > finalized:
                    continue
                self.state["matches"][match.key] = {**match.to_dict(),
                                                    "decimals": match.decimals}
                new += 1
                self._audit("MATCH_PROVEN", match.to_dict())
            for item in rejections:
                self.state["rejections"].append(item)
                self._audit("CANDIDATE_REJECTED", item)
            self.state["scanned_transactions"] = int(self.state["scanned_transactions"]) + 1
        self.state["next_block"] = scanned_to + 1
        self.state["finalized_block_at_scan"] = finalized
        self._save()
        return {"from": start, "to": scanned_to, "finalized": finalized,
                "candidates": len(candidates), "new_matches": new, "source": source.name}

    # -- attribution -------------------------------------------------------------------
    def _unapplied(self) -> list[RedemptionMatch]:
        out = []
        for key, data in self.state["matches"].items():
            if key in self.state["applied"]:
                continue
            match = RedemptionMatch.from_dict(data)
            if self.manager.already_credited(match.tx_hash, match.log_index):
                continue
            out.append(match)
        return out

    def _attribute(self, report: dict, *, apply: bool) -> None:
        ledger = self.manager.ledger
        unapplied = self._unapplied()
        groups: dict[str, list] = {}
        for match in unapplied:
            groups[match.key] = [
                ep for ep in ledger.episodes.values()
                if ep.state in HOLDER_STATES
                and match.covers(ep.condition_id, ep.outcome_index, ep.token_id)
            ]
        claimed: dict[str, list[str]] = {}
        for key, eps in groups.items():
            for ep in eps:
                claimed.setdefault(ep.episode_id, []).append(key)
        unresolved: dict[str, dict] = {}
        for match in unapplied:
            eps = groups[match.key]
            reason = self._attribution_problem(match, eps, claimed)
            if reason is not None:
                unresolved[match.key] = {"reason": reason, "match": match.to_dict(),
                                         "episodes": [e.episode_id for e in eps]}
                continue
            ids = [ep.episode_id for ep in eps]
            if not apply:
                report.setdefault("would_apply", []).append(
                    {"match": match.to_dict(), "episodes": ids,
                     "allocation": {e.episode_id: str(e.receivable) for e in eps}})
                continue
            ok, reasons = self.manager.apply_redemption(match, ids, discovery={
                "discovered_by": "redemption_discovery",
                "candidate_source": self.state.get("source")})
            if ok:
                entry = {"episodes": ids, "match": match.to_dict(),
                         "allocation": {e.episode_id: str(ledger.episodes[e.episode_id].credited)
                                        for e in eps},
                         "applied_at": datetime.now(timezone.utc).isoformat()}
                self.state["applied"][match.key] = entry
                report["applied"].append(entry)
                self._audit("REDEMPTION_APPLIED", entry)
                self.record("REDEMPTION_DISCOVERED", entry)
            else:
                unresolved[match.key] = {"reason": ";".join(reasons), "match": match.to_dict(),
                                         "episodes": ids}
        changed = set(unresolved) != set(self.state["unresolved"]) or any(
            self.state["unresolved"].get(k, {}).get("reason") != v["reason"]
            for k, v in unresolved.items())
        self.state["unresolved"] = unresolved
        if changed and unresolved:
            self._audit("UNRESOLVED", {"items": unresolved})
            self.record("REDEMPTION_DISCOVERY_UNRESOLVED",
                        {k: v["reason"] for k, v in unresolved.items()})

    def _attribution_problem(self, match: RedemptionMatch, eps: list,
                             claimed: dict[str, list[str]]) -> str | None:
        if not eps:
            return "NO_UNSETTLED_LEDGER_EPISODE"
        if any(len(claimed.get(ep.episode_id, ())) > 1 for ep in eps):
            return "MULTIPLE_REDEMPTIONS_COVER_SAME_EPISODES"
        waiting = [ep for ep in eps if ep.state is not S.REDEMPTION_PENDING_OPERATOR]
        if waiting:
            return "HOLDER_NOT_YET_AWAITING_REDEMPTION:" + ",".join(
                sorted({ep.state.value for ep in waiting}))
        if match.positions:
            for token, raw in match.positions.items():
                held = [ep for ep in eps if _same_token(ep.token_id, token)]
                if not held:
                    continue
                ledger_raw = sum((ep.net_shares for ep in held), Decimal(0)) * (
                    Decimal(10) ** match.decimals)
                if ledger_raw != Decimal(raw):
                    return f"SURRENDERED_SHARES_DIFFER_FROM_LEDGER:{token[-8:]}"
        total = sum((ep.receivable for ep in eps), Decimal(0))
        if abs(total - match.credited) > Decimal("0.000002") * len(eps):
            return f"CREDIT_AMOUNT_MISMATCH:{match.credited}!={total}"
        return None


def _same_token(token_id: str, decimal_id: str) -> bool:
    try:
        return str(int(token_id)) == decimal_id
    except (TypeError, ValueError):
        return False
