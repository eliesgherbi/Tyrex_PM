from decimal import Decimal as D

import pytest

from tyrex_pm.market_data.reconcile import reconcile_top


def test_crossing_insert_prunes_only_obsolete_levels():
    bids = {D('.52'): D('2'), D('.49'): D('3')}
    asks = {D('.50'): D('1'), D('.53'): D('4'), D('.55'): D('7')}
    assert reconcile_top(bids, asks, D('.52'), D('.53')) == 1
    assert asks == {D('.53'): D('4'), D('.55'): D('7')}
    assert bids[D('.52')] == D('2')


def test_missing_depth_is_never_invented():
    with pytest.raises(ValueError, match='absent'):
        reconcile_top({D('.49'): D('3')}, {D('.55'): D('7')}, D('.50'), D('.55'))


@pytest.mark.parametrize('bid,ask', [('.52', '.51'), ('.52', '.52')])
def test_cross_or_lock_remains_unavailable(bid, ask):
    with pytest.raises(ValueError):
        reconcile_top({D(bid): D('2')}, {D(ask): D('3')}, D(bid), D(ask))


def test_missing_reports_only_validate_structure_and_empty_sentinels_clear_sides():
    bids, asks = {D('.4'): D('1')}, {D('.6'): D('2')}
    assert reconcile_top(bids, asks) == 0
    assert reconcile_top(bids, asks, D('0'), D('1')) == 2
    assert bids == asks == {}
