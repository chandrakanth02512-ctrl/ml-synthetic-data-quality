import copy
from dataclasses import replace
from unittest.mock import Mock

import numpy as np
import pytest

from synthetic_quality.evaluation import evaluate_predictions
from synthetic_quality.schema import REQUIRED_COLUMNS
from synthetic_quality_lakehouse.config import canonical, gcs_parts
from synthetic_quality_vertex import inputs, train
from synthetic_quality_vertex.config import ObjectRef, TrainingConfig, decode_rows
from tests_vertex.conftest import artifact, reference, split


def test_all_detectors_reports_predictions_and_reload(trained, store):
    metrics = artifact(store, trained, "metrics")
    metadata = artifact(store, trained, "metadata")
    predictions = decode_rows(store.verify(trained["artifacts"]["predictions"]))
    assert (trained["training_rows"], trained["evaluation_rows"]) == (256, 100)
    assert set(metrics) == {"rule_based", "isolation_forest", "combined_or"}
    assert metadata["model_reload_verified"] is True
    actual = [r["actual_anomaly"] for r in predictions]
    assert sum(actual) == 5
    for approach in metrics:
        result = evaluate_predictions(actual, [r[approach + "_prediction"] for r in predictions])
        assert metrics[approach] == result
        assert np.shape(result["confusion_matrix"]) == (2, 2)
        assert all(k in result for k in ("precision", "recall", "f1_score"))
    assert all(
        r["combined_or_prediction"]
        == max(r["rule_based_prediction"], r["isolation_forest_prediction"])
        for r in predictions
    )
    assert store.verify(trained["artifacts"]["metrics_csv"]).startswith(b"detector,")


def test_completed_replay_never_retrains(trained, store, config, monkeypatch):
    monkeypatch.setattr(train, "train_model", Mock(side_effect=AssertionError("no retrain")))
    before = copy.deepcopy(store.client.buckets["offline-data"].objects)
    assert train.run(config, store) == trained
    assert store.client.buckets["offline-data"].objects == before


def test_independent_repeated_training_is_deterministic(trained, store, config):
    second = train.run(replace(config, run_id="vertex-002"), store)
    for key in ("model", "baseline", "metrics", "metrics_csv"):
        assert store.verify(second["artifacts"][key]) == store.verify(trained["artifacts"][key])


def test_labels_read_only_after_fit_and_never_enter_features(config, store, monkeypatch):
    events = []
    original_fit = train.train_model
    original_truth = inputs.truth_labels

    def fit(frame, settings):
        assert list(frame.columns) == list(REQUIRED_COLUMNS)
        events.append("fit")
        return original_fit(frame, settings)

    def truth(*args):
        assert events == ["fit"]
        events.append("truth")
        return original_truth(*args)

    monkeypatch.setattr(train, "train_model", fit)
    monkeypatch.setattr(inputs, "truth_labels", truth)
    train.run(config, store)
    assert events == ["fit", "truth"]


def test_changed_labels_change_only_evaluation_not_model(trained, config, store):
    rows = decode_rows(store.read(config.truth.uri)[0])
    for row in rows:
        row["is_anomaly"] = 1 - row["is_anomaly"]
    ref = reference(
        store,
        "gs://offline-data/lakehouse/vertex/truth/flipped.ndjson",
        b"".join(canonical(r) + b"\n" for r in rows),
    )
    second = train.run(replace(config, run_id="flipped-001", truth=ref), store)
    assert trained["artifacts"]["model"]["sha256"] == second["artifacts"]["model"]["sha256"]
    assert artifact(store, trained, "metrics") != artifact(store, second, "metrics")


def test_partial_failure_preserves_artifacts_and_recovers(config, store):
    bucket = store.client.buckets["offline-data"]
    bucket.fail_before = gcs_parts(config.root + "/evaluation.json")[1]
    with pytest.raises(RuntimeError, match="upload"):
        train.run(config, store)
    model_before = store.read(config.root + "/isolation_forest.joblib")
    assert store.optional(config.root + "/completion.json") is None
    train.run(config, store)
    assert store.read(config.root + "/isolation_forest.joblib") == model_before


def test_lost_completion_ack_reconciles_without_training(config, store, monkeypatch):
    store.client.buckets["offline-data"].lose_ack = gcs_parts(config.root + "/completion.json")[1]
    with pytest.raises(RuntimeError, match="acknowledgement"):
        train.run(config, store)
    monkeypatch.setattr(train, "train_model", Mock(side_effect=AssertionError()))
    assert train.run(config, store)["status"] == "COMPLETE"


def test_changed_config_and_corrupt_completed_artifact_stop(trained, config, store):
    with pytest.raises(ValueError, match="conflict"):
        train.run(replace(config, baseline_quantile=0.99), store)
    bucket = store.client.buckets["offline-data"]
    name = gcs_parts(trained["artifacts"]["model"]["uri"])[1]
    bucket.objects[name] = b"bad", 999
    with pytest.raises(ValueError, match="changed"):
        train.run(config, store)


@pytest.mark.parametrize("fault", ["ids", "dates", "labels", "invalid"])
def test_bad_splits_fail_before_training(fault, config, data_pair, store, monkeypatch):
    _, evaluation = data_pair
    frame = evaluation.transactions.copy()
    extra = None
    if fault == "ids":
        frame.loc[0, "transaction_id"] = data_pair[0].transactions.transaction_id.iloc[0]
    elif fault == "dates":
        frame["timestamp"] = frame["timestamp"] - np.timedelta64(31, "D")
    elif fault == "labels":
        extra = {"is_anomaly": 1}
    else:
        frame.loc[0, "transaction_amount"] = -1
    ref = split(store, "bad-evaluation", frame, extra)
    fit = Mock(side_effect=AssertionError("must fail before fitting"))
    monkeypatch.setattr(train, "train_model", fit)
    with pytest.raises(ValueError):
        train.run(replace(config, evaluation=ref), store)
    fit.assert_not_called()


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "extra", "nonbinary", "boolean", "schema"]
)
def test_truth_must_align_exactly(fault, config, store):
    rows = decode_rows(store.read(config.truth.uri)[0])
    if fault == "missing":
        rows.pop()
    if fault == "duplicate":
        rows.append(dict(rows[0]))
    if fault == "extra":
        rows.append({"transaction_id": "unknown", "is_anomaly": 0})
    if fault == "nonbinary":
        rows[0]["is_anomaly"] = 2
    if fault == "boolean":
        rows[0]["is_anomaly"] = True
    if fault == "schema":
        rows[0]["training_label"] = 1
    ref = reference(
        store,
        "gs://offline-data/lakehouse/vertex/truth/bad.ndjson",
        b"".join(canonical(r) + b"\n" for r in rows),
    )
    with pytest.raises(ValueError):
        train.run(replace(config, truth=ref), store)


def test_reordered_truth_preserves_metrics(trained, config, store):
    data = b"\n".join(reversed(store.read(config.truth.uri)[0].splitlines())) + b"\n"
    ref = reference(store, "gs://offline-data/lakehouse/vertex/truth/reordered.ndjson", data)
    result = train.run(replace(config, run_id="reordered-001", truth=ref), store)
    assert artifact(store, result, "metrics") == artifact(store, trained, "metrics")


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": "../x"},
        {"max_rows": 10001},
        {"output_uri": "gs://gcp-idq-data-984592102391/lakehouse/vertex"},
        {"baseline_quantile": 1},
    ],
)
def test_configuration_rejects_unsafe_or_unbounded_settings(config, changes):
    with pytest.raises(ValueError):
        replace(config, **changes)


def test_bad_reference_and_manifest_rejected(config, store):
    with pytest.raises(ValueError):
        ObjectRef(config.truth.uri, 0, "f" * 64)
    with pytest.raises((TypeError, ValueError)):
        TrainingConfig.from_dict({**config.to_dict(), "unexpected": True})
    with pytest.raises(ValueError):
        train.run(replace(config, training=replace(config.training, sha256="0" * 64)), store)


def test_clean_split_size_limit_stops_before_fitting(config, store, monkeypatch):
    fit = Mock(side_effect=AssertionError())
    monkeypatch.setattr(train, "train_model", fit)
    with pytest.raises(ValueError, match="bound"):
        train.run(replace(config, max_rows=10), store)
    fit.assert_not_called()


def test_model_reload_mismatch_prevents_publication(config, store, monkeypatch):
    original = train.predict_anomalies
    count = 0

    def predictions(*args):
        nonlocal count
        count += 1
        result = original(*args)
        if count == 2:
            return replace(result, labels=1 - result.labels)
        return result

    monkeypatch.setattr(train, "predict_anomalies", predictions)
    with pytest.raises(ValueError, match="reload"):
        train.run(config, store)
    assert store.optional(config.root + "/completion.json") is None
