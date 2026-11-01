"""CPU training/evaluation with immutable GCS artifacts; labels never enter fitting."""

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from synthetic_quality.baseline import RuleBaseline
from synthetic_quality.evaluation import evaluate_predictions, save_evaluation_reports
from synthetic_quality.model import load_model, predict_anomalies, save_model, train_model
from synthetic_quality.schema import REQUIRED_COLUMNS
from synthetic_quality_lakehouse.config import canonical
from synthetic_quality_vertex import inputs
from synthetic_quality_vertex.config import TrainingConfig, fingerprint, request

ARTIFACTS = {
    "model": "isolation_forest.joblib",
    "baseline": "rule_baseline.json",
    "metadata": "model_metadata.json",
    "metrics": "evaluation.json",
    "metrics_csv": "evaluation.csv",
    "predictions": "predictions.ndjson",
}


def validate_completion(manifest):
    config = TrainingConfig.from_dict(manifest["request"]["config"])
    if (
        manifest.get("status") != "COMPLETE"
        or manifest.get("format_version") != 1
        or manifest["fingerprint"] != fingerprint(manifest["request"])
        or set(manifest["artifacts"]) != set(ARTIFACTS)
        or type(manifest["evaluation_rows"]) is not int
        or not 1 <= manifest["evaluation_rows"] <= config.max_rows
        or type(manifest["training_rows"]) is not int
        or not 2 <= manifest["training_rows"] <= config.max_rows
    ):
        raise ValueError("Invalid Vertex completion identity/counts")
    for key, record in manifest["artifacts"].items():
        if record["uri"] != config.root + "/" + ARTIFACTS[key]:
            raise ValueError("Unexpected Vertex artifact path")
        from synthetic_quality_vertex.config import ObjectRef

        ObjectRef(record["uri"], record["generation"], record["sha256"])
    return config


def run(config, store):
    identity = request(config)
    run_hash = fingerprint(identity)
    store.create(config.root + "/request.json", canonical(identity))
    previous = store.optional(config.root + "/completion.json")
    if previous:
        complete = json.loads(previous[0])
        validate_completion(complete)
        if complete["request"] != identity:
            raise ValueError("Completed run identity differs")
        for record in complete["artifacts"].values():
            store.verify(record)
        return complete
    train, evaluation, training_manifest, evaluation_manifest = inputs.splits(store, config)
    training_data = train[list(REQUIRED_COLUMNS)]
    evaluation_data = evaluation[list(REQUIRED_COLUMNS)]
    # No labels are read until fitting, rule calibration and predictions are finished.
    model = train_model(training_data, config.model)
    baseline = RuleBaseline.fit(training_data, config.baseline_quantile)
    ml = predict_anomalies(model, evaluation_data)
    rule = baseline.predict(evaluation_data)
    combined = np.maximum(rule, ml.labels)
    actual = inputs.truth_labels(store, config, evaluation)
    metrics = {
        name: evaluate_predictions(actual, values)
        for name, values in {
            "rule_based": rule,
            "isolation_forest": ml.labels,
            "combined_or": combined,
        }.items()
    }
    with TemporaryDirectory(prefix="vertex-quality-") as directory:
        root = Path(directory)
        model_path = save_model(model, root / ARTIFACTS["model"])
        restored = predict_anomalies(load_model(model_path), evaluation_data)
        if not (
            np.array_equal(restored.labels, ml.labels)
            and np.array_equal(restored.scores, ml.scores)
        ):
            raise ValueError("Model reload changed predictions")
        model_sha = hashlib.sha256(model_path.read_bytes()).hexdigest()
        scored = pd.DataFrame(
            {
                "run_id": config.run_id,
                "source_fingerprint": run_hash,
                "transaction_id": evaluation.transaction_id,
                "source_row_id": evaluation.source_row_id,
                "source_record_sha256": evaluation.source_record_sha256,
                "anomaly_score": ml.scores,
                "rule_based_prediction": rule,
                "isolation_forest_prediction": ml.labels,
                "combined_or_prediction": combined,
                "actual_anomaly": actual,
                "model_sha256": model_sha,
            }
        )
        # DataFrame.to_json truncates floats; standard JSON preserves IEEE double round trips.
        prediction_data = b"".join(canonical(r) + b"\n" for r in scored.to_dict("records"))
        metadata = {
            "request": identity,
            "fingerprint": run_hash,
            "training_fingerprint": training_manifest["fingerprint"],
            "evaluation_fingerprint": evaluation_manifest["fingerprint"],
            "training_rows": len(train),
            "evaluation_rows": len(evaluation),
            "training_quarantined": training_manifest["artifacts"]["quarantine"]["row_count"],
            "evaluation_quarantined": evaluation_manifest["artifacts"]["quarantine"]["row_count"],
            "model_reload_verified": True,
            "model_sha256": model_sha,
            "feature_names": model.named_steps["preprocess"].get_feature_names_out().tolist(),
            "score_definition": "negative decision_function; larger is more anomalous",
            "baseline": asdict(baseline),
            "evaluation": metrics,
        }
        save_evaluation_reports(metrics, root)
        payloads = {
            "model": model_path.read_bytes(),
            "baseline": canonical(asdict(baseline)),
            "metadata": canonical(metadata),
            "metrics": (root / "evaluation.json").read_bytes(),
            "metrics_csv": (root / "evaluation.csv").read_bytes(),
            "predictions": prediction_data,
        }
        records = {
            key: store.create(config.root + "/" + ARTIFACTS[key], value)
            for key, value in payloads.items()
        }
    complete = {
        "format_version": 1,
        "status": "COMPLETE",
        "request": identity,
        "fingerprint": run_hash,
        "training_rows": len(train),
        "evaluation_rows": len(evaluation),
        "artifacts": records,
    }
    validate_completion(complete)
    store.create(config.root + "/completion.json", canonical(complete))
    return complete


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = TrainingConfig.from_dict(json.loads(args.config.read_text()))
    from google.cloud import storage

    from synthetic_quality_lakehouse.storage import GCSObjects

    result = run(config, GCSObjects(storage.Client()))
    print(
        json.dumps(
            {
                "status": result["status"],
                "run_id": config.run_id,
                "completion_uri": config.root + "/completion.json",
            }
        )
    )


if __name__ == "__main__":
    main()
