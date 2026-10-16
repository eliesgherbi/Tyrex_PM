"""Safeguards for the second half of the offline BTC5m course."""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")
pytest.importorskip("pyarrow")

from tyrex_pm.research.tuto_btc5m.advanced import (  # noqa: E402
    FEATURES,
    chronological_split,
    classifier_demo,
    clustered_effect,
    combine_datasets,
    digital_probability,
    ev_size,
    feature_dataset,
    local_block_means,
    reference_grid,
)
from tyrex_pm.research.tuto_btc5m.data import NS  # noqa: E402

T = 1790869500 * NS
IID = "polymarket:137:outcome:1"


def sample_fixture():
    datasets = {"book_snapshots": [], "trade_prints": [], "operational_events": []}
    for i in range(90):
        base = {
            "received_at": T + i * NS,
            "session_id": "session",
            "connection_id": "socket",
            "ingest_sequence": i,
            "payload_path": "$",
            "record_id": str(i),
        }
        bid = Decimal("0.40") + Decimal([0, 1, 3, 2, 1, 0][i % 6]) / 100
        datasets["book_snapshots"].append(
            {
                **base,
                "source_id": "clob",
                "instrument_id": IID,
                "snapshot_origin": "websocket",
                "provider_update_id": None,
                "bids": [{"price": bid, "quantity": Decimal("10")}],
                "asks": [{"price": bid + Decimal("0.02"), "quantity": Decimal("20")}],
            }
        )
        datasets["trade_prints"].append(
            {
                **base,
                "source_id": "binance",
                "instrument_id": "binance:spot:BTCUSDT",
                "price": Decimal(str(100 + 0.1 * i + 0.2 * np.sin(i))),
                "quantity": Decimal("1"),
            }
        )
    sample = SimpleNamespace(
        start=T,
        end=T + 90 * NS,
        labels={IID: "Up"},
        manifest={"market_id": "m1", "instrument_ids": [IID]},
        manifest_sha256="test_manifest",
        records=lambda name: datasets.get(name, []),
    )

    def frame(name):
        out = pd.DataFrame(datasets.get(name, []))
        for column in ("received_at", "source_time"):
            if column in out:
                out[column] = pd.to_datetime(out[column], unit="ns", utc=True)
        return out

    sample.frame = frame
    return sample, datasets


def test_reference_receipt_join_masks_disconnect_even_for_queued_fresh_tick():
    sample, datasets = sample_fixture()
    datasets["reference_observations"] = [
        {
            "received_at": T + i * NS,
            "source_time": T + i * NS,
            "feed_id": "ref",
            "source_id": "polybolt",
            "value": Decimal(100 + i),
        }
        for i in [1, 2, 4]
    ]
    datasets["operational_events"] = [
        {
            "received_at": T + 2 * NS,
            "details": json.dumps({"type": "disconnected", "source": "polybolt"}),
        },
        {
            "received_at": T + 4 * NS,
            "details": json.dumps({"type": "connected", "source": "polybolt"}),
        },
    ]
    times = pd.date_range(pd.Timestamp(T, tz="UTC"), periods=6, freq="1s")
    result = reference_grid(sample, times, max_age_s=5)
    assert result.reference_eligible.tolist() == [False, True, False, False, True, True]
    assert result.reference_value.iloc[1] == 101
    assert pd.isna(result.reference_value.iloc[2])
    assert (result.reference_at.dropna() <= result.time[result.reference_at.notna()]).all()
    assert not reference_grid(sample, times, max_age_s=0).reference_eligible.iloc[-1]


def test_probability_symmetry_missing_variance_and_zero_variance_tie():
    assert digital_probability(0, 0.01) == 0.5
    assert digital_probability(0.1, 0.01) + digital_probability(-0.1, 0.01) == pytest.approx(1)
    assert digital_probability(0, 0) == 1
    assert digital_probability(-0.1, 0) == 0
    assert np.isnan(digital_probability(np.nan, 0.01))
    with pytest.raises(ValueError, match="negative"):
        digital_probability(0, -0.1)


def test_sizing_exact_cents_cap_and_single_contract_correlated_risk():
    result = ev_size(0.75, 0.70)
    assert result["shares_for_target_EV"] == 60
    assert result["expected_profit_USD"] == pytest.approx(3)
    assert result["profit_if_lose_USD"] == pytest.approx(-42)
    assert result["pnl_standard_deviation_USD"] == pytest.approx(60 * np.sqrt(0.75 * 0.25))
    capped = ev_size(0.75, 0.70, cash_cap_usd=7)
    assert capped["shares_with_cash_cap"] == 10
    assert not capped["target_feasible_under_cap"]
    assert ev_size(0.70, 0.70)["shares_with_cash_cap"] == 0
    assert ev_size(0.75, 0.70, fee_usd_per_share=0.01)["shares_for_target_EV"] == 75


def test_feature_prefix_unchanged_by_future_prices_and_labels_missing_at_end():
    sample, datasets = sample_fixture()
    before = feature_dataset(sample)
    assert before.target_rise.iloc[-5:].isna().all()
    assert before.forward_move_cents.iloc[-5:].isna().all()
    assert before.loc[before.forward_move_cents.isna(), "target_rise"].isna().all()
    assert not before.features_ready.iloc[:20].any()
    for row in datasets["trade_prints"][60:]:
        row["price"] = Decimal("9999")
    for row in datasets["book_snapshots"][60:]:
        row["bids"] = [{"price": Decimal("0.80"), "quantity": Decimal("100")}]
        row["asks"] = [{"price": Decimal("0.82"), "quantity": Decimal("5")}]
    after = feature_dataset(sample)
    pd.testing.assert_frame_equal(before.loc[:59, list(FEATURES)], after.loc[:59, list(FEATURES)])
    assert before.attrs["settlement_labels_used"] is False


def test_single_window_split_is_named_demo_and_purges_overlapping_lookbacks():
    frame = feature_dataset(sample_fixture()[0])
    with pytest.raises(ValueError, match="two different markets"):
        chronological_split(frame)
    split = chronological_split(frame, mode="within_window_demo")
    assert not split["independent_new_market_holdout"]
    assert split["purged_rows"] > 0
    assert split["train"].label_end.max() < split["test"].feature_start.min()


def test_cross_market_holdout_has_disjoint_ids_and_rejects_duplicate_samples():
    one = feature_dataset(sample_fixture()[0])
    with pytest.raises(ValueError, match="Duplicate"):
        combine_datasets([one, one.copy()])
    two = one.copy()
    two["market_id"] = "m2"
    for column in ("time", "window_start", "window_end", "feature_start", "label_end"):
        two[column] += pd.Timedelta(minutes=5)
    split = chronological_split(combine_datasets([one, two]))
    assert split["independent_new_market_holdout"]
    assert set(split["train"].market_id) == {"m1"}
    assert set(split["test"].market_id) == {"m2"}


def test_classifier_scaler_fit_only_on_train_and_forbids_target_features():
    pytest.importorskip("sklearn")
    split = chronological_split(feature_dataset(sample_fixture()[0]), mode="within_window_demo")
    # A distribution shift in held-out features must not change training preprocessing.
    split["test"]["mid"] = 0.99
    result = classifier_demo(split)
    scaler = result["pipeline"].named_steps["standardscaler"]
    np.testing.assert_allclose(scaler.mean_, split["train"][list(FEATURES)].mean().to_numpy())
    assert result["probability_target"] == "next_midpoint_rise_not_settlement_or_profit"
    with pytest.raises(ValueError, match="causal"):
        classifier_demo(split, features=("forward_move_cents",))


def test_single_training_class_falls_back_to_baseline_without_crashing():
    pytest.importorskip("sklearn")
    split = chronological_split(feature_dataset(sample_fixture()[0]), mode="within_window_demo")
    split["train"]["target_rise"] = 1
    result = classifier_demo(split)
    assert result["pipeline"] is None
    assert result["predictions"].constant_training_prior.eq(1).all()
    assert len(result["metrics"]) == 1


def test_block_resampling_does_not_fabricate_adjacency_across_missing_rows():
    times = pd.date_range("2026-10-01", periods=5, freq="1s", tz="UTC")
    values = pd.Series([1, np.nan, 3, np.nan, 5], index=times)
    with pytest.raises(ValueError, match="consecutive block"):
        local_block_means(values, block_length=2)
    with pytest.raises(ValueError, match="clock"):
        local_block_means(values.dropna())
    a = local_block_means(values, block_length=1, repetitions=10)
    np.testing.assert_array_equal(a, local_block_means(values, block_length=1, repetitions=10))


def test_one_window_declines_independent_window_confidence_interval():
    frame = pd.DataFrame(
        {
            "market_id": ["one"] * 100,
            "imbalance": [0.5, -0.5] * 50,
            "forward_move_cents": [2, -1] * 50,
        }
    )
    result = clustered_effect(frame)
    assert result["effect_cents"] == 3
    assert result["market_count"] == 1
    assert result["interval"] is None
    assert result["status"] == "insufficient_independent_windows"
