from decimal import Decimal
from types import SimpleNamespace

import pytest

from tyrex_pm.adapters.polymarket.sdk_secure import balance_allowance_to_decimal


def test_balance_allowance_adapter_converts_sdk_base_units() -> None:
    balance, allowance = balance_allowance_to_decimal(
        SimpleNamespace(
            balance=5_500_000,
            allowances={"exchange-a": 10_000_000, "exchange-b": 8_000_000},
        ),
        conditional=False,
    )
    assert balance == Decimal("5.5")
    assert allowance == Decimal("8")


@pytest.mark.parametrize(
    "payload",
    [None, SimpleNamespace(), SimpleNamespace(balance=0, allowances=None)],
)
def test_missing_balance_observation_never_becomes_zero(payload) -> None:  # noqa: ANN001
    with pytest.raises(ValueError):
        balance_allowance_to_decimal(payload, conditional=False)
