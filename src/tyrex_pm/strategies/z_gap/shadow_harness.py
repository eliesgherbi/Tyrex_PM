"""Shadow enforce E2E harness for Z-Gap Phase A (A0.8)."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.quant.fees import parse_fee_model_from_raw
from tyrex_pm.quant.volatility import EwmaVolatilityEstimator
from tyrex_pm.runtime.config import (
    Z_GAP_ENTRY_MODE_ENFORCE,
    parse_app_config,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.time_authority import TimeAuthority
from tyrex_pm.runtime.z_gap_enforce import (
    ZGapEnforceRuntimeState,
    run_enforce_tick,
    run_z_gap_enforce_loop,
)
from tyrex_pm.runtime.z_gap_run import ObserveTickContext, sigma_config_from_zg
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.signal_state_store import FRESHNESS_OBSERVED, SignalStateStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.z_gap.facts import ZGapObserveRuntimeState
from tyrex_pm.strategies.z_gap.state import ZGapLifecycleState
from tyrex_pm.strategies.z_gap.strategy import ZGapStrategy

FD_RAW = {"c": "0xabc", "fd": {"r": 0.07, "e": 1, "to": True}}
DEFAULT_TICK = Decimal("0.01")


@dataclass
class TickSpec:
    now_ts: float
    binance_price: Decimal | None = None
    event_end_ts: float | None = None
    wallet_venue_qty: Decimal | None = None


@dataclass
class ShadowHarnessResult:
    enforce_state: ZGapEnforceRuntimeState
    facts: list[dict[str, Any]]
    oms_submits: list[dict[str, Any]]
    exit_code: int


def write_preflight_artifacts(path: Path, *, market_id: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (path / "fee_curve_spike.json").write_text(
        json.dumps({"outcome": "full_curve_available", "fd": {"r": 0.07, "e": 1}}),
        encoding="utf-8",
    )
    (path / "binance_connectivity.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    (path / "ptb_attestation.json").write_text(
        json.dumps(
            {
                "attestation_pass": True,
                "ptb_error_bps": "0.1",
                "golden_fixture_only": False,
                "enforce_unlock_allowed": True,
            }
        ),
        encoding="utf-8",
    )
    (path / "clock_sanity.json").write_text(
        json.dumps(
            {
                "sync_status": "synced",
                "enforce_gate_pass": True,
                "time_authority_uncertainty_ms": 20.0,
            }
        ),
        encoding="utf-8",
    )
    (path / "calibration_lite_review.json").write_text(
        json.dumps(
            {
                "reviewed": True,
                "reviewed_by": "operator",
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
                "status": "accepted_for_tiny_live",
                "operator_signoff": True,
            }
        ),
        encoding="utf-8",
    )
    (path / "operator_enforce_approval.json").write_text(
        json.dumps(
            {
                "approved": True,
                "market_id": market_id,
                "maximum_usd": "5",
                "one_trade_only": True,
                "approval_ts": now,
                "expiration_ts": now + 3600,
            }
        ),
        encoding="utf-8",
    )


def base_shadow_app(
    *,
    event_start_ts: float,
    event_end_ts: float,
    exit_cfg: dict[str, Any] | None = None,
    live_validation: dict[str, Any] | None = None,
    live_like_risk: bool = False,
    reconciliation: dict[str, Any] | None = None,
) -> Any:
    exit_block = {
        "z_stop": "0.25",
        "stop_confirm_s": 0,
        "flatten_before_event_end_s": 20,
        "retry_interval_ms": 0,
        "max_exit_attempts": 5,
    }
    if exit_cfg:
        exit_block.update(exit_cfg)
    zg = {
        "entry_mode": "enforce",
        "market_id": "btc_5m_20260709_1200",
        "condition_id": "0xabc",
        "yes_token_id": "111",
        "no_token_id": "222",
        "event_start_ts": event_start_ts,
        "event_end_ts": event_end_ts,
        "sigma": {"min_samples_s": 1, "sample_interval_s": 0, "jump_guard": False},
        "entry": {
            "theta_take": "0.05",
            "theta_fill_floor": "0.03",
            "z_band": ["0.8", "2.2"],
            "tau_band_s": [60, 210],
            "basis_max_bps": "3",
            "expected_slippage_ticks": 1,
        },
        "sizing": {"mode": "fixed_usd", "max_usd": "5", "min_shares": "5"},
        "exit": exit_block,
    }
    if live_validation:
        zg["live_validation"] = live_validation
    if reconciliation:
        zg["reconciliation"] = reconciliation
    return parse_app_config(
        risk={
            "notional": {"min_usd": "0.01", "max_usd": "5", "max_policy": "deny"},
            "deployment": {"token_cap_usd": "25", "portfolio_cap_usd": "100"},
            "venue_min_size": {"enabled": False},
            "capital": {"enabled": False, "max_wallet_age_s": 120},
            "inventory": {"sell_requires_venue_position": live_like_risk},
            "concurrency": {"max_orders_in_flight": 4},
            "readiness": {
                "require_wallet_sync": False,
                "max_wallet_age_s_live": 120,
                "require_heartbeat_live": False,
                "require_user_ws_live": False,
            },
        },
        strategy={"kind": "z_gap", "enabled": True, "z_gap": zg},
        runtime={
            "execution_mode": "shadow",
            "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
            "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
            "market_data": {"enabled": True, "max_book_age_s": 30},
            "allocation_ledger": {"enabled": True},
            "signal_feeds": {"enabled": False},
        },
    )


def build_coord(app: Any) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(
        wallet=WalletStore(),
        orders=OrderStore(),
        health=HealthRuntime(),
        allocation_ledger=AllocationLedger(),
    )
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)
    coord.signal_state = SignalStateStore()
    coord.market_state = MarketStateStore(default_max_age_s=30.0)
    return coord


def populate_market_books(coord: RuntimeCoordinator, *, ts: datetime) -> None:
    coord.market_state.apply_snapshot(
        make_snapshot(
            TokenId("111"),
            bids=[(Decimal("0.60"), Decimal("1000"))],
            asks=[(Decimal("0.61"), Decimal("1000"))],
            ts=ts,
        )
    )
    coord.market_state.apply_snapshot(
        make_snapshot(
            TokenId("222"),
            bids=[(Decimal("0.38"), Decimal("1000"))],
            asks=[(Decimal("0.39"), Decimal("1000"))],
            ts=ts,
        )
    )


def update_signal(
    coord: RuntimeCoordinator,
    *,
    binance_price: Decimal,
    ptb: Decimal,
    ts: datetime,
) -> None:
    store = coord.signal_state
    assert store is not None
    store.update_binance(binance_price, source_ts=ts, recv_ts=ts, stream="bookTicker")
    store.update_chainlink(binance_price - Decimal("1"), source_ts=ts, recv_ts=ts)
    store.update_price_to_beat(ptb, status=FRESHNESS_OBSERVED, observed_ts=ts, lag_ms=100.0)
    store.mark_binance_connected(connected=True)
    store.mark_chainlink_connected(connected=True)


def synced_time_authority(*, epoch_at_sync: float) -> TimeAuthority:
    return TimeAuthority(
        sync_status="synced",
        offset_ms=0.0,
        uncertainty_ms=20.0,
        median_offset_ms=0.0,
        samples_requested=5,
        samples_kept=5,
        max_rtt_ms=50.0,
        _epoch_at_sync=epoch_at_sync,
        _mono_at_sync=time.monotonic(),
    )


class ListSink:
    def __init__(self) -> None:
        self.facts: list[dict[str, Any]] = []

    def write(self, obj: dict[str, Any]) -> None:
        self.facts.append(obj)


@dataclass
class ShadowHarness:
    app: Any
    coord: RuntimeCoordinator
    run_id: RunId
    sink: ListSink
    strategy: ZGapStrategy
    oms: Any
    enforce_state: ZGapEnforceRuntimeState
    estimator: EwmaVolatilityEstimator
    fee_model: Any
    time_authority: TimeAuthority
    preflight_dir: Path
    _prev_preflight_dir: str | None = None

    def _restore_preflight_env(self) -> None:
        if self._prev_preflight_dir is None:
            os.environ.pop("Z_GAP_PREFLIGHT_DIR", None)
        else:
            os.environ["Z_GAP_PREFLIGHT_DIR"] = self._prev_preflight_dir

    @classmethod
    def create(
        cls,
        *,
        tmp_path: Path,
        event_start_ts: float,
        event_end_ts: float,
        oms: Any,
        exit_cfg: dict[str, Any] | None = None,
        live_like_risk: bool = False,
        reconciliation: dict[str, Any] | None = None,
    ) -> ShadowHarness:
        preflight_dir = tmp_path / "preflight"
        market_id = "btc_5m_20260709_1200"
        write_preflight_artifacts(preflight_dir, market_id=market_id)
        prev_preflight_dir = os.environ.get("Z_GAP_PREFLIGHT_DIR")
        os.environ["Z_GAP_PREFLIGHT_DIR"] = str(preflight_dir)
        lifecycle_path = preflight_dir / "lifecycle_state.json"
        if lifecycle_path.exists():
            lifecycle_path.unlink()

        app = base_shadow_app(
            event_start_ts=event_start_ts,
            event_end_ts=event_end_ts,
            exit_cfg=exit_cfg,
            live_like_risk=live_like_risk,
            reconciliation=reconciliation,
        )

        coord = build_coord(app)
        estimator = EwmaVolatilityEstimator(config=sigma_config_from_zg(app.z_gap))
        for i in range(5):
            estimator.update(Decimal("100000") + Decimal(i), datetime.fromtimestamp(event_start_ts - 60, tz=timezone.utc))

        ta = synced_time_authority(epoch_at_sync=event_start_ts - 120)
        fee_model = parse_fee_model_from_raw(FD_RAW, condition_id=app.z_gap.condition_id, market_id=app.z_gap.market_id)
        lc = ZGapLifecycleState(
            market_id=app.z_gap.market_id,
            condition_id=app.z_gap.condition_id,
            owner_id=app.z_gap.owner_id,
        )
        return cls(
            app=app,
            coord=coord,
            run_id=RunId("z_gap_shadow_e2e"),
            sink=ListSink(),
            strategy=ZGapStrategy(app.z_gap),
            oms=oms,
            enforce_state=ZGapEnforceRuntimeState(lifecycle=lc, observe=ZGapObserveRuntimeState()),
            estimator=estimator,
            fee_model=fee_model,
            time_authority=ta,
            preflight_dir=preflight_dir,
            _prev_preflight_dir=prev_preflight_dir,
        )

    async def run_ticks(self, ticks: list[TickSpec]) -> ShadowHarnessResult:
        try:
            return await self._run_ticks_inner(ticks)
        finally:
            self._restore_preflight_env()

    async def _run_ticks_inner(self, ticks: list[TickSpec]) -> ShadowHarnessResult:
        zg = self.app.z_gap
        exit_cfg = zg.exit or self.app.z_gap.exit
        from tyrex_pm.runtime.config import _parse_z_gap_exit

        parsed_exit = exit_cfg if exit_cfg is not None else _parse_z_gap_exit(None)

        for spec in ticks:
            if spec.event_end_ts is not None:
                zg = replace(zg, event_end_ts=spec.event_end_ts)
                self.app = replace(self.app, z_gap=zg)

            ts = datetime.fromtimestamp(spec.now_ts, tz=timezone.utc)
            price = spec.binance_price or Decimal("100100")
            if spec.wallet_venue_qty is not None:
                lc = self.enforce_state.lifecycle
                if lc is not None and lc.token_id:
                    from tyrex_pm.core.models import WalletPosition

                    self.coord.wallet.positions[TokenId(lc.token_id)] = WalletPosition(
                        token_id=TokenId(lc.token_id),
                        qty=spec.wallet_venue_qty,
                    )
            update_signal(self.coord, binance_price=price, ptb=Decimal("100000"), ts=ts)
            populate_market_books(self.coord, ts=ts)
            self.estimator.update(price, ts)

            ctx = ObserveTickContext(
                app=self.app,
                zg=zg,
                run_id=self.run_id,
                coord=self.coord,
                sink=self.sink,
                state=self.enforce_state.observe,
                estimator=self.estimator,
                fee_model=self.fee_model,
                time_authority=self.time_authority,
                entry_cfg=zg.entry,
                now_ts=spec.now_ts,
                lifecycle=self.enforce_state.lifecycle,
                tick_size=DEFAULT_TICK,
            )
            self.enforce_state.observe.fee_model_status = getattr(self.fee_model, "fee_model_status", "resolved")
            self.enforce_state.observe.fee_model_id = getattr(self.fee_model, "fee_model_id", "polymarket_dynamic_fd_v1")
            await run_enforce_tick(
                ctx=ctx,
                enforce_state=self.enforce_state,
                strategy=self.strategy,
                oms=self.oms,
                exit_cfg=parsed_exit,
                apply_local_shadow_fill=False,
                preflight_manual=False,
            )
            lc = self.enforce_state.lifecycle
            if lc is not None and lc.is_terminal():
                break

        from tyrex_pm.runtime.z_gap_enforce import _operational_pass_enforce, _persist_lifecycle
        from tyrex_pm.strategies.z_gap import facts as zg_facts
        from tyrex_pm.strategies.z_gap.lifecycle import allocated_quantity

        lc = self.enforce_state.lifecycle
        alloc_zero = True
        venue_qty = Decimal("0")
        recon_status = self.enforce_state.last_reconciliation_status
        if lc is not None and lc.token_id:
            alloc_zero = allocated_quantity(self.coord, owner_id=lc.owner_id, token_id=lc.token_id) == 0
            from tyrex_pm.strategies.z_gap.reconciliation import venue_reported_quantity

            venue_qty = venue_reported_quantity(self.coord, lc.token_id)

        operational = _operational_pass_enforce(self.enforce_state)
        zg_facts.emit_z_gap_enforce_terminal_summary(
            self.sink,
            self.run_id,
            self.enforce_state.observe,
            lifecycle=lc,
            entry_mode=Z_GAP_ENTRY_MODE_ENFORCE,
            market_id=zg.market_id,
            condition_id=zg.condition_id,
            signal=self.enforce_state.observe.last_signal,
            operational_pass=operational,
            allocation_zero=alloc_zero,
            venue_reported_quantity=venue_qty,
            reconciliation_status=recon_status,
            requested_entry_quantity=lc.entry_requested_shares if lc else Decimal("0"),
            filled_entry_quantity=lc.entry_filled_shares if lc else Decimal("0"),
            allocated_quantity=allocated_quantity(self.coord, owner_id=lc.owner_id, token_id=lc.token_id)
            if lc and lc.token_id
            else Decimal("0"),
        )
        if lc is not None:
            _persist_lifecycle(lc)

        exit_code = 0 if operational else 1
        submits = getattr(self.oms, "submits", [])
        return ShadowHarnessResult(
            enforce_state=self.enforce_state,
            facts=self.sink.facts,
            oms_submits=list(submits),
            exit_code=exit_code,
        )

    def fact_types(self) -> set[str]:
        return {f["fact_type"] for f in self.sink.facts}

    def terminal_summary(self) -> dict[str, Any] | None:
        for f in reversed(self.sink.facts):
            if f.get("fact_type") == "z_gap_terminal_summary":
                return f.get("payload", {})
        return None
