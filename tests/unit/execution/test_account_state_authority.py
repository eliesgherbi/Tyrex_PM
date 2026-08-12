from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from tyrex_pm.execution.account_state import (
    AccountSnapshotStatus,
    AccountStateAuthority,
    AccountStatePolicy,
)


class _AccountGateway:
    def __init__(self, *, collateral_failures: int = 0) -> None:
        self.collateral_failures = collateral_failures
        self.collateral_calls = 0

    async def collateral_balance(self):
        self.collateral_calls += 1
        if self.collateral_calls <= self.collateral_failures:
            raise TimeoutError("collateral read timed out")
        return Decimal("20"), Decimal("20")

    async def conditional_balance(self, _token_id):
        return Decimal("0"), Decimal("100")

    async def list_open_orders(self, **_kwargs):
        return ()


class _InstrumentedAccountGateway:
    def __init__(self) -> None:
        self.entered = 0
        self.active = 0
        self.maximum_active = 0

    async def _read(self, value):  # noqa: ANN001
        self.entered += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        await asyncio.sleep(0.001)
        self.active -= 1
        return value

    async def collateral_balance(self):
        return await self._read((Decimal("20"), Decimal("20")))

    async def conditional_balance(self, _token_id):
        return await self._read((Decimal("0"), Decimal("100")))

    async def list_open_orders(self, **_kwargs):
        return await self._read(())


def _policy(**overrides) -> AccountStatePolicy:
    values = {
        "refresh_interval_s": 10.0,
        "snapshot_max_age_s": 10.0,
        "read_timeout_s": 0.1,
        "retry_attempts": 2,
        "retry_base_delay_s": 0.001,
    }
    values.update(overrides)
    return AccountStatePolicy(**values)


async def _wait_for_status(
    authority: AccountStateAuthority,
    expected: AccountSnapshotStatus,
) -> None:
    for _ in range(100):
        if authority.current("condition").status is expected:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"account state did not become {expected.value}")


@pytest.mark.asyncio
async def test_failed_read_is_unavailable_not_zero_balance() -> None:
    events: list[tuple[str, dict]] = []
    authority = AccountStateAuthority(
        _AccountGateway(collateral_failures=99),
        policy=_policy(),
        evidence_sink=lambda kind, payload: events.append((kind, payload)),
    )
    authority.prepare(
        market_id="condition",
        token_ids=("yes", "no"),
        required_collateral=Decimal("5"),
    )
    for _ in range(100):
        if len([kind for kind, _payload in events if kind == "ACCOUNT_STATE_READ_FAILED"]) == 2:
            break
        await asyncio.sleep(0.005)
    snapshot = authority.current("condition")
    assert snapshot.status is AccountSnapshotStatus.UNAVAILABLE
    assert snapshot.collateral_balance is None
    assert snapshot.collateral_allowance is None
    assert not snapshot.read_complete
    assert not any("INSUFFICIENT_COLLATERAL" in value for value in snapshot.blockers)
    failures = [payload for kind, payload in events if kind == "ACCOUNT_STATE_READ_FAILED"]
    assert len(failures) == 2
    assert failures[0]["retry_scheduled"] is True
    assert failures[-1]["retry_scheduled"] is False
    await authority.close()


@pytest.mark.asyncio
async def test_transient_read_recovers_without_market_rebind() -> None:
    gateway = _AccountGateway(collateral_failures=1)
    events: list[tuple[str, dict]] = []
    authority = AccountStateAuthority(
        gateway,
        policy=_policy(),
        evidence_sink=lambda kind, payload: events.append((kind, payload)),
    )
    authority.prepare(
        market_id="condition",
        token_ids=("yes", "no"),
        required_collateral=Decimal("5"),
    )
    await _wait_for_status(authority, AccountSnapshotStatus.READY)
    snapshot = authority.current("condition")
    assert snapshot.status is AccountSnapshotStatus.READY
    assert snapshot.collateral_balance == Decimal("20")
    assert snapshot.token("yes").balance_shares == Decimal("0")
    assert gateway.collateral_calls == 2
    assert [kind for kind, _payload in events].count("ACCOUNT_STATE_READ_FAILED") == 1
    assert [kind for kind, _payload in events].count("ACCOUNT_STATE_UPDATED") == 1
    await authority.close()


@pytest.mark.asyncio
async def test_account_observations_are_serialized_on_the_shared_sdk_client() -> None:
    gateway = _InstrumentedAccountGateway()
    authority = AccountStateAuthority(gateway, policy=_policy())
    authority.prepare(
        market_id="condition",
        token_ids=("yes", "no"),
        required_collateral=Decimal("5"),
    )
    await _wait_for_status(authority, AccountSnapshotStatus.READY)
    assert gateway.entered == 4
    assert gateway.maximum_active == 1
    await authority.close()


@pytest.mark.asyncio
async def test_prepared_market_promotion_keeps_refresh_task_alive() -> None:
    gateway = _AccountGateway()
    authority = AccountStateAuthority(
        gateway,
        policy=_policy(refresh_interval_s=10.0),
    )
    authority.prepare(
        market_id="condition",
        token_ids=("yes", "no"),
        required_collateral=Decimal("5"),
        active=False,
    )
    await _wait_for_status(authority, AccountSnapshotStatus.READY)
    first_calls = gateway.collateral_calls

    authority.prepare(
        market_id="condition",
        token_ids=("yes", "no"),
        required_collateral=Decimal("5"),
        active=True,
    )
    for _ in range(100):
        if gateway.collateral_calls > first_calls:
            break
        await asyncio.sleep(0.005)
    assert gateway.collateral_calls > first_calls
    assert authority._targets["condition"].active is True
    assert not authority._tasks["condition"].done()
    await authority.close()


@pytest.mark.asyncio
async def test_slow_account_read_is_reported_without_force_cancellation() -> None:
    class SlowGateway(_AccountGateway):
        async def collateral_balance(self):
            await asyncio.sleep(0.02)
            return Decimal("20"), Decimal("20")

    events: list[tuple[str, dict]] = []
    authority = AccountStateAuthority(
        SlowGateway(),
        policy=_policy(read_timeout_s=0.005),
        evidence_sink=lambda kind, payload: events.append((kind, payload)),
    )
    authority.prepare(
        market_id="condition",
        token_ids=("yes", "no"),
        required_collateral=Decimal("5"),
    )
    await _wait_for_status(authority, AccountSnapshotStatus.READY)
    updated = next(payload for kind, payload in events if kind == "ACCOUNT_STATE_UPDATED")
    assert "COLLATERAL" in updated["slow_operations"]
    assert "COLLATERAL" in updated["operation_queue_wait_ms"]
    assert "refresh_cycle_overrun" in updated
    assert authority.current("condition").status is AccountSnapshotStatus.READY
    await authority.close()


@pytest.mark.asyncio
async def test_last_good_snapshot_expires_to_explicit_stale_state() -> None:
    authority = AccountStateAuthority(
        _AccountGateway(),
        policy=_policy(snapshot_max_age_s=0.01),
    )
    authority.prepare(
        market_id="condition",
        token_ids=("yes", "no"),
        required_collateral=Decimal("5"),
    )
    await _wait_for_status(authority, AccountSnapshotStatus.READY)
    await asyncio.sleep(0.015)
    snapshot = authority.current("condition")
    assert snapshot.status is AccountSnapshotStatus.STALE
    assert snapshot.collateral_balance == Decimal("20")
    assert "ACCOUNT_SNAPSHOT_STALE" in snapshot.blockers
    assert not snapshot.read_complete
    await authority.close()


@pytest.mark.asyncio
async def test_hold_ready_snapshot_keeps_ready_during_submit_critical_section() -> None:
    authority = AccountStateAuthority(
        _AccountGateway(),
        policy=_policy(snapshot_max_age_s=0.01, refresh_interval_s=0.05),
    )
    authority.prepare(
        market_id="condition",
        token_ids=("yes", "no"),
        required_collateral=Decimal("5"),
        active=True,
    )
    await _wait_for_status(authority, AccountSnapshotStatus.READY)
    async with authority.hold_ready_snapshot("condition"):
        await asyncio.sleep(0.03)
        held = authority.current("condition")
        assert held.status is AccountSnapshotStatus.READY
        assert held.read_complete
    await asyncio.sleep(0.02)
    aged = authority.current("condition")
    assert aged.status in {AccountSnapshotStatus.STALE, AccountSnapshotStatus.READY}
    await authority.close()
