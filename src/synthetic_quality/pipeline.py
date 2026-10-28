"""One-command generation, validation, training, evaluation, and artifact export."""

import argparse
import hashlib
import platform
from dataclasses import asdict, dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from synthetic_quality.baseline import RuleBaseline
from synthetic_quality.data_generator import generate_transactions
from synthetic_quality.evaluation import evaluate_predictions, save_evaluation_reports, write_json
from synthetic_quality.model import (
    ModelConfig,
    load_model,
    predict_anomalies,
    save_model,
    train_model,
)
from synthetic_quality.quality_checks import validate_transactions
from synthetic_quality.schema import UNITS_PER_USD


@dataclass(frozen=True)
class PipelineConfig:
    training_rows: int = 5_000
    evaluation_rows: int = 1_000
    training_seed: int = 42
    evaluation_seed: int = 123
    anomaly_fraction: float = 0.05
    baseline_quantile: float = 0.995
    model: ModelConfig = field(default_factory=ModelConfig)

    def __post_init__(self) -> None:
        if self.training_seed == self.evaluation_seed:
            raise ValueError("Use different training and evaluation seeds to preserve independence")


def run_pipeline(
    output_directory: str | Path = ".", config: PipelineConfig | None = None
) -> dict[str, Any]:
    settings = config or PipelineConfig()
    output = Path(output_directory)
    data_directory = output / "data"
    model_directory = output / "models"
    report_directory = output / "reports"
    for directory in (data_directory, model_directory, report_directory):
        directory.mkdir(parents=True, exist_ok=True)

    training = generate_transactions(
        settings.training_rows,
        settings.training_seed,
        dataset_name="training",
        start_date="2025-01-01",
    )
    evaluation = generate_transactions(
        settings.evaluation_rows,
        settings.evaluation_seed,
        settings.anomaly_fraction,
        dataset_name="evaluation",
        start_date="2025-02-01",
    )
    quality = {
        "training": validate_transactions(training.transactions),
        "evaluation": validate_transactions(evaluation.transactions),
    }
    write_json(report_directory / "data_quality.json", {k: v.to_dict() for k, v in quality.items()})
    quality_rows = [
        {
            "dataset": dataset,
            "check_name": check.check_name,
            "failure_count": check.failure_count,
            "passed": check.passed,
            "skipped_columns": ",".join(check.skipped_columns),
        }
        for dataset, report in quality.items()
        for check in report.checks
    ]
    pd.DataFrame(quality_rows).to_csv(
        report_directory / "data_quality.csv", index=False, lineterminator="\n"
    )
    for dataset, report in quality.items():
        report.raise_for_errors(dataset)

    exported_data = {
        "training.csv": training.transactions,
        "evaluation.csv": evaluation.transactions,
        "evaluation_truth.csv": evaluation.truth,
    }
    data_hashes = {}
    for filename, frame in exported_data.items():
        path = data_directory / filename
        frame.to_csv(path, index=False, lineterminator="\n")
        data_hashes[filename] = hashlib.sha256(path.read_bytes()).hexdigest()

    model = train_model(training.transactions, settings.model)
    ml_predictions = predict_anomalies(model, evaluation.transactions)
    model_path = save_model(model, model_directory / "isolation_forest.joblib")
    restored_predictions = predict_anomalies(load_model(model_path), evaluation.transactions)
    if not (
        np.array_equal(ml_predictions.labels, restored_predictions.labels)
        and np.array_equal(ml_predictions.scores, restored_predictions.scores)
    ):
        raise RuntimeError("Saved model round-trip changed predictions or scores")

    baseline = RuleBaseline.fit(training.transactions, settings.baseline_quantile)
    baseline_predictions = baseline.predict(evaluation.transactions)
    predictions = {
        "rule_based": baseline_predictions,
        "isolation_forest": ml_predictions.labels,
        "combined_or": np.maximum(baseline_predictions, ml_predictions.labels),
    }
    # Align truth explicitly by ID, independently of its existing row order.
    aligned_truth = evaluation.truth.set_index("transaction_id").loc[
        evaluation.transactions["transaction_id"]
    ]
    actual = aligned_truth["is_anomaly"].to_numpy()
    results = {name: evaluate_predictions(actual, values) for name, values in predictions.items()}
    save_evaluation_reports(results, report_directory)
    scored = pd.DataFrame(
        {
            "transaction_id": evaluation.transactions["transaction_id"].to_numpy(),
            "actual_anomaly": actual,
            "injection_type": aligned_truth["injection_type"].to_numpy(),
            "anomaly_score": ml_predictions.scores,
            **{f"{name}_prediction": values for name, values in predictions.items()},
        }
    )
    scored.to_csv(
        report_directory / "predictions.csv", index=False, float_format="%.12g", lineterminator="\n"
    )
    manifest = {
        "config": asdict(settings),
        "dataset_sha256": data_hashes,
        "python_version": platform.python_version(),
        "package_versions": {
            package: version(package)
            for package in (
                "ml-synthetic-data-quality",
                "numpy",
                "pandas",
                "scikit-learn",
                "joblib",
            )
        },
        "synthetic_units_per_usd": UNITS_PER_USD,
        "training_period": "2025-01-01 through 2025-01-28 UTC",
        "evaluation_period": "2025-02-01 through 2025-02-28 UTC",
        "rule_baseline": asdict(baseline),
        "score_definition": "negative decision_function; larger is more anomalous; anomaly if > 0",
        "feature_names": model.named_steps["preprocess"].get_feature_names_out().tolist(),
        "model_reload_verified": True,
    }
    write_json(report_directory / "run_manifest.json", manifest)
    write_json(model_directory / "model_metadata.json", manifest)
    write_json(model_directory / "rule_baseline.json", asdict(baseline))
    return {
        "evaluation": results,
        "quality": {k: v.to_dict() for k, v in quality.items()},
        "manifest": manifest,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    parser.add_argument("--n-estimators", type=int, default=200)
    parser.add_argument("--max-samples", type=int, default=256)
    parser.add_argument("--contamination", type=float, default=0.02)
    parser.add_argument("--model-seed", type=int, default=42)
    args = parser.parse_args(argv)
    model_config = ModelConfig(
        n_estimators=args.n_estimators,
        max_samples=args.max_samples,
        contamination=args.contamination,
        random_state=args.model_seed,
    )
    result = run_pipeline(args.output_dir, PipelineConfig(model=model_config))
    print("Data quality: training PASS; evaluation PASS. Saved-model reload: PASS.")
    print("Evaluation: 1,000 transactions; 50 injected anomalies.")
    for detector, metrics in result["evaluation"].items():
        print(
            f"{detector:18} precision={metrics['precision']:.4f} "
            f"recall={metrics['recall']:.4f} F1={metrics['f1_score']:.4f} "
            f"FP={metrics['false_positives']} FN={metrics['false_negatives']}"
        )
    print(f"Artifacts: {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
