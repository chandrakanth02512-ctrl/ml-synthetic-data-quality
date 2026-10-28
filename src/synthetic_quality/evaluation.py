"""Fixed binary-label metrics and deterministic JSON/CSV report writers."""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import confusion_matrix, f1_score, precision_score, recall_score


def evaluate_predictions(
    actual: Sequence[int] | np.ndarray, predicted: Sequence[int] | np.ndarray
) -> dict[str, Any]:
    truth = np.asarray(actual)
    predictions = np.asarray(predicted)
    if truth.ndim != 1 or predictions.ndim != 1 or len(truth) != len(predictions):
        raise ValueError("Actual and predicted labels must be equally sized one-dimensional arrays")
    if len(truth) == 0:
        raise ValueError("Cannot evaluate an empty dataset")
    if not np.isin(truth, [0, 1]).all() or not np.isin(predictions, [0, 1]).all():
        raise ValueError("Labels must be binary: 0=normal, 1=anomaly")
    matrix = confusion_matrix(truth, predictions, labels=[0, 1])
    tn, fp, fn, tp = (int(value) for value in matrix.ravel())
    return {
        "precision": float(precision_score(truth, predictions, zero_division=0)),
        "recall": float(recall_score(truth, predictions, zero_division=0)),
        "f1_score": float(f1_score(truth, predictions, zero_division=0)),
        "confusion_matrix": matrix.tolist(),
        "confusion_matrix_layout": "rows=actual [0,1], columns=predicted [0,1]",
        "total_transactions": len(truth),
        "total_actual_anomalies": int(truth.sum()),
        "total_predicted_anomalies": int(predictions.sum()),
        "true_negatives": tn,
        "false_positives": fp,
        "false_negatives": fn,
        "true_positives": tp,
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )


def save_evaluation_reports(
    results: Mapping[str, dict[str, Any]], report_directory: str | Path
) -> tuple[Path, Path]:
    directory = Path(report_directory)
    json_path = directory / "evaluation.json"
    csv_path = directory / "evaluation.csv"
    write_json(json_path, results)
    rows = []
    for detector, metrics in results.items():
        row = {"detector": detector, **metrics}
        row["confusion_matrix"] = json.dumps(row["confusion_matrix"])
        rows.append(row)
    pd.DataFrame(rows).to_csv(csv_path, index=False, float_format="%.10f", lineterminator="\n")
    return json_path, csv_path
