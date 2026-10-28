import numpy as np
import pandas as pd
import pytest

from synthetic_quality.data_generator import generate_transactions
from synthetic_quality.quality_checks import validate_transactions
from synthetic_quality.schema import REQUIRED_COLUMNS, UNITS_PER_USD


def test_same_seed_reproduces_transactions_and_truth():
    first = generate_transactions(1_000, 123, 0.05)
    second = generate_transactions(1_000, 123, 0.05)
    pd.testing.assert_frame_equal(first.transactions, second.transactions)
    pd.testing.assert_frame_equal(first.truth, second.truth)


def test_different_seeds_change_values_not_just_identifiers():
    first = generate_transactions(100, 42).transactions
    second = generate_transactions(100, 43).transactions
    assert not np.array_equal(first["transaction_amount"], second["transaction_amount"])
    assert not np.array_equal(first["timestamp"], second["timestamp"])


@pytest.mark.parametrize("size", [1, 1_000, 5_000])
def test_dataset_size_and_exact_schema(size):
    dataset = generate_transactions(size, 42)
    assert len(dataset.transactions) == len(dataset.truth) == size
    assert tuple(dataset.transactions.columns) == REQUIRED_COLUMNS
    assert dataset.transactions["transaction_id"].is_unique
    assert str(dataset.transactions["timestamp"].dt.tz) == "UTC"
    assert dataset.transactions["transaction_id"].equals(dataset.truth["transaction_id"])


def test_training_has_no_injections():
    training = generate_transactions(5_000, 42)
    assert training.truth["is_anomaly"].sum() == 0
    assert set(training.truth["injection_type"]) == {"normal"}


def test_injected_labels_match_modifications_and_pass_quality():
    dataset = generate_transactions(1_000, 123, 0.05)
    transactions, truth = dataset.transactions, dataset.truth
    assert truth["is_anomaly"].sum() == 50
    assert truth["injection_type"].value_counts().to_dict() == {
        "normal": 950,
        "high_amount": 25,
        "unusual_time": 25,
    }
    np.testing.assert_array_equal(truth["is_anomaly"], truth["injection_type"].ne("normal"))
    usd = transactions["transaction_amount"] / transactions["currency"].map(UNITS_PER_USD)
    assert usd[truth["injection_type"].eq("high_amount")].between(1_999.99, 5_000.01).all()
    hours = transactions["timestamp"].dt.hour
    assert hours[truth["injection_type"].eq("unusual_time")].between(0, 4).all()
    assert hours[truth["injection_type"].eq("normal")].between(7, 22).all()
    assert validate_transactions(transactions).passed


def test_default_splits_have_disjoint_ids_and_periods():
    training = generate_transactions(5_000, 42, dataset_name="training")
    evaluation = generate_transactions(
        1_000,
        123,
        0.05,
        dataset_name="evaluation",
        start_date="2025-02-01",
    )
    assert set(training.transactions["transaction_id"]).isdisjoint(
        evaluation.transactions["transaction_id"]
    )
    assert training.transactions["timestamp"].max() < evaluation.transactions["timestamp"].min()


def test_generator_does_not_modify_numpy_global_random_state():
    np.random.seed(17)
    expected = np.random.random(4)
    np.random.seed(17)
    generate_transactions(100, 42, 0.05)
    np.testing.assert_array_equal(np.random.random(4), expected)


@pytest.mark.parametrize("size", [0, -1, 1.5, True])
def test_invalid_sizes_raise(size):
    with pytest.raises(ValueError, match="positive integer"):
        generate_transactions(size, 42)


@pytest.mark.parametrize("fraction", [-0.1, 1.1, float("nan"), float("inf")])
def test_invalid_anomaly_fraction_raises(fraction):
    with pytest.raises(ValueError, match="anomaly_fraction"):
        generate_transactions(100, 42, fraction)


def test_fraction_rounding_and_all_anomalies():
    assert generate_transactions(21, 42, 0.05).truth["is_anomaly"].sum() == 1
    assert generate_transactions(11, 42, 1.0).truth["is_anomaly"].sum() == 11


def test_invalid_split_name_and_date_raise():
    with pytest.raises(ValueError, match="dataset_name"):
        generate_transactions(10, 42, dataset_name="../invalid")
    with pytest.raises(ValueError, match="start_date"):
        generate_transactions(10, 42, start_date="NaT")
