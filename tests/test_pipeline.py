import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from synthetic_quality import pipeline
from synthetic_quality.data_generator import GeneratedDataset
from synthetic_quality.model import ModelConfig, load_model, predict_anomalies
from synthetic_quality.pipeline import PipelineConfig, run_pipeline


@pytest.fixture(scope="module")
def completed_run(tmp_path_factory):
    output = tmp_path_factory.mktemp("full_pipeline")
    return output, run_pipeline(output)


def test_full_default_pipeline_writes_all_required_artifacts(completed_run):
    output, result = completed_run
    expected = [
        "data/training.csv",
        "data/evaluation.csv",
        "data/evaluation_truth.csv",
        "models/isolation_forest.joblib",
        "models/model_metadata.json",
        "models/rule_baseline.json",
        "reports/data_quality.json",
        "reports/data_quality.csv",
        "reports/evaluation.json",
        "reports/evaluation.csv",
        "reports/predictions.csv",
        "reports/run_manifest.json",
    ]
    assert all(
        (output / path).is_file() and (output / path).stat().st_size > 0 for path in expected
    )
    assert all(report["passed"] for report in result["quality"].values())
    assert set(result["evaluation"]) == {"rule_based", "isolation_forest", "combined_or"}
    for metrics in result["evaluation"].values():
        assert metrics["total_actual_anomalies"] == 50
        assert metrics["total_transactions"] == 1_000
        assert sum(sum(row) for row in metrics["confusion_matrix"]) == 1_000
    assert result["manifest"]["model_reload_verified"] is True
    training = pd.read_csv(output / "data/training.csv")
    assert len(training) == 5_000
    assert "is_anomaly" not in training


def test_full_runs_reproduce_data_predictions_reports_and_hashes(completed_run, tmp_path):
    original, expected = completed_run
    assert run_pipeline(tmp_path) == expected
    for directory in ("data", "reports"):
        for path in (original / directory).iterdir():
            assert path.read_bytes() == (tmp_path / directory / path.name).read_bytes()
    for filename, checksum in expected["manifest"]["dataset_sha256"].items():
        assert hashlib.sha256((original / "data" / filename).read_bytes()).hexdigest() == checksum


def test_saved_model_reuse_from_exported_csv(completed_run):
    output, _ = completed_run
    model = load_model(output / "models/isolation_forest.joblib")
    evaluation = pd.read_csv(output / "data/evaluation.csv")
    evaluation["timestamp"] = pd.to_datetime(evaluation["timestamp"], utc=True)
    predictions = predict_anomalies(model, evaluation)
    saved = pd.read_csv(output / "reports/predictions.csv")
    np.testing.assert_array_equal(predictions.labels, saved["isolation_forest_prediction"])
    np.testing.assert_allclose(predictions.scores, saved["anomaly_score"], atol=1e-11)
    np.testing.assert_array_equal(
        saved["combined_or_prediction"],
        np.maximum(saved["rule_based_prediction"], saved["isolation_forest_prediction"]),
    )


def test_bad_data_writes_quality_failure_then_stops_before_training(monkeypatch, tmp_path):
    generate = pipeline.generate_transactions

    def invalid(*args, **kwargs):
        dataset = generate(*args, **kwargs)
        dataset.transactions.loc[0, "transaction_amount"] = -10
        return dataset

    def forbidden_training(*args, **kwargs):
        pytest.fail("Training must never run after validation failure")

    monkeypatch.setattr(pipeline, "generate_transactions", invalid)
    monkeypatch.setattr(pipeline, "train_model", forbidden_training)
    with pytest.raises(ValueError, match="invalid_transaction_amounts=1"):
        run_pipeline(tmp_path)
    quality = json.loads((tmp_path / "reports/data_quality.json").read_text(encoding="utf-8"))
    assert quality["training"]["passed"] is False
    assert not (tmp_path / "models/isolation_forest.joblib").exists()


def test_truth_order_does_not_change_evaluation(monkeypatch, tmp_path):
    config = replace(
        PipelineConfig(), training_rows=100, evaluation_rows=100, model=ModelConfig(n_estimators=10)
    )
    expected = run_pipeline(tmp_path / "original", config)
    generate = pipeline.generate_transactions

    def shuffled_truth(*args, **kwargs):
        dataset = generate(*args, **kwargs)
        return GeneratedDataset(dataset.transactions, dataset.truth.sample(frac=1, random_state=19))

    monkeypatch.setattr(pipeline, "generate_transactions", shuffled_truth)
    actual = run_pipeline(tmp_path / "shuffled", config)
    assert actual["evaluation"] == expected["evaluation"]


def test_same_training_and_evaluation_seed_is_rejected():
    with pytest.raises(ValueError, match="different training and evaluation seeds"):
        PipelineConfig(training_seed=42, evaluation_seed=42)


def test_documented_module_entrypoint_executes(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "synthetic_quality.pipeline", "--output-dir", str(tmp_path)],
        text=True,
        capture_output=True,
        check=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert "training PASS; evaluation PASS" in result.stdout
    assert "Saved-model reload: PASS" in result.stdout
    assert "isolation_forest" in result.stdout
    assert (tmp_path / "reports/evaluation.json").is_file()
