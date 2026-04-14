"""Step 2 DoD: WalletSyncConfig, WalletSyncResult, UnresolvableEntry — frozen, slotted, importable."""

from __future__ import annotations

from tyrex_pm.runtime.wallet_sync import (
    UnresolvableEntry,
    WalletSyncConfig,
    WalletSyncResult,
)


def test_wallet_sync_config_defaults() -> None:
    c = WalletSyncConfig()
    assert c.poll_interval_seconds == 15.0
    assert c.startup_deadline_seconds == 120.0
    assert c.per_instrument_max_retries == 3
    assert c.data_api_base_url == "https://data-api.polymarket.com"


def test_wallet_sync_config_frozen() -> None:
    c = WalletSyncConfig()
    try:
        c.poll_interval_seconds = 5.0  # type: ignore[misc]
        raise AssertionError("should be frozen")
    except AttributeError:
        pass


def test_wallet_sync_result_all_fields() -> None:
    r = WalletSyncResult(
        cycle_number=1,
        positions_fetched=5,
        orders_fetched=3,
        condition_ids_on_wallet=4,
        condition_ids_in_cache=3,
        instruments_newly_added=2,
        resolution_failures=1,
        unresolvable_retrying=1,
        unresolvable_terminal=0,
        http_positions_ok=True,
        http_orders_ok=True,
        first_sync_complete=True,
        elapsed_seconds=0.5,
        failure_details={"clob_error_string": 1},
    )
    assert r.cycle_number == 1
    assert r.unresolvable_retrying == 1
    assert r.http_positions_ok is True
    assert r.first_sync_complete is True


def test_unresolvable_entry_frozen() -> None:
    e = UnresolvableEntry(
        condition_id="0xabc",
        token_ids=("t1",),
        last_detail="clob_error_string",
        retry_count=3,
        terminal=True,
    )
    assert e.terminal is True
    assert e.retry_count == 3
    try:
        e.terminal = False  # type: ignore[misc]
        raise AssertionError("should be frozen")
    except AttributeError:
        pass
