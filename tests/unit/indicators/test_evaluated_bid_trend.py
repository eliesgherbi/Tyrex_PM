from decimal import Decimal as D

from tyrex_pm.indicators.evaluated_bid_trend import EvaluatedBidTrend


def trend():
    return EvaluatedBidTrend(lookback_s=2, min_drop='.02', confirmation_s=1, max_gap_s=1.5)


def test_causal_anchor_confirmation_and_rebound():
    t = trend()
    assert not t.observe(0, D('.70'))['ready']
    assert not t.observe(1, D('.69'))['ready']
    first = t.observe(2, D('.67'))
    assert first['drop'] == '0.03' and not first['confirmed']
    assert t.observe(3, D('.66'))['confirmed']
    assert not t.observe(4, D('.69'))['confirmed']


def test_gaps_bad_quotes_epochs_and_restarts_require_warmup():
    t = trend()
    for i in range(4):
        result = t.observe(i, D('.70') - D('.02') * i, epoch=1)
    assert result['confirmed']
    assert not t.observe(6, D('.50'), epoch=1)['ready']
    assert not t.observe(7, D('.49'), epoch=2)['ready']
    assert not t.observe(8, None, ready=False, epoch=2)['ready']
    assert not trend().observe(8, D('.49'))['ready']


def test_confirmation_uses_elapsed_time_not_sample_count():
    t = trend()
    t.observe(0, D('.70'))
    t.observe(1, D('.69'))
    t.observe(2, D('.66'))
    assert not t.observe(2.1, D('.65'))['confirmed']
    assert not t.observe(2.2, D('.64'))['confirmed']
    assert t.observe(3, D('.63'))['confirmed']
