import joblib
import numpy as np
import pytest

from synthetic_quality.model import (
    ModelConfig,
    load_model,
    predict_anomalies,
    save_model,
    train_model,
)


def test_training_and_prediction_contract(fitted_model, evaluation_dataset):
    predictions = predict_anomalies(fitted_model, evaluation_dataset.transactions)
    assert len(fitted_model.named_steps["detector"].estimators_) == 40
    assert (
        len(predictions.labels) == len(predictions.scores) == len(evaluation_dataset.transactions)
    )
    assert set(predictions.labels).issubset({0, 1})
    assert np.isfinite(predictions.scores).all()
    np.testing.assert_array_equal(predictions.labels, predictions.scores > 0)


def test_same_configuration_produces_identical_predictions(normal_transactions, model_config):
    first = predict_anomalies(train_model(normal_transactions, model_config), normal_transactions)
    second = predict_anomalies(train_model(normal_transactions, model_config), normal_transactions)
    np.testing.assert_array_equal(first.labels, second.labels)
    np.testing.assert_array_equal(first.scores, second.scores)


def test_training_ignores_even_deliberately_corrupted_targets(normal_transactions, model_config):
    enriched = normal_transactions.assign(is_anomaly=1, injection_type="fraud", injection_flag=100)
    first = predict_anomalies(train_model(normal_transactions, model_config), normal_transactions)
    second = predict_anomalies(train_model(enriched, model_config), normal_transactions)
    np.testing.assert_array_equal(first.scores, second.scores)


def test_model_round_trip_preserves_pipeline_labels_and_scores(
    fitted_model, evaluation_dataset, tmp_path
):
    path = save_model(fitted_model, tmp_path / "nested" / "model.joblib")
    restored = load_model(path)
    expected = predict_anomalies(fitted_model, evaluation_dataset.transactions)
    actual = predict_anomalies(restored, evaluation_dataset.transactions)
    np.testing.assert_array_equal(actual.labels, expected.labels)
    np.testing.assert_array_equal(actual.scores, expected.scores)


def test_single_row_predictions_match_batch_without_future_information(
    fitted_model, evaluation_dataset
):
    batch = predict_anomalies(fitted_model, evaluation_dataset.transactions)
    single = predict_anomalies(fitted_model, evaluation_dataset.transactions.iloc[[7]])
    np.testing.assert_array_equal(single.scores, batch.scores[7:8])


def test_invalid_saved_payload_is_rejected(tmp_path):
    path = tmp_path / "wrong.joblib"
    joblib.dump({"wrong": "object"}, path)
    with pytest.raises(ValueError, match="model pipeline"):
        load_model(path)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"n_estimators": 0},
        {"max_samples": 0},
        {"n_estimators": 1.5},
        {"max_samples": True},
        {"contamination": 0},
        {"contamination": 0.6},
        {"contamination": float("nan")},
    ],
)
def test_invalid_model_configuration(kwargs):
    with pytest.raises(ValueError):
        ModelConfig(**kwargs)
