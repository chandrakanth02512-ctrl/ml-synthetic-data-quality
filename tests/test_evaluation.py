import json

import numpy as np
import pandas as pd
import pytest

from synthetic_quality.evaluation import evaluate_predictions, save_evaluation_reports


def test_hand_calculated_precision_recall_f1_and_confusion_matrix():
    result = evaluate_predictions([1, 1, 1, 1, 0, 0, 0, 0], [1, 1, 0, 0, 1, 0, 0, 0])
    assert result["precision"] == pytest.approx(2 / 3)
    assert result["recall"] == pytest.approx(1 / 2)
    assert result["f1_score"] == pytest.approx(4 / 7)
    assert result["confusion_matrix"] == [[3, 1], [2, 2]]
    assert result["total_actual_anomalies"] == 4
    assert result["total_predicted_anomalies"] == 3
    assert result["false_positives"] == 1
    assert result["false_negatives"] == 2


@pytest.mark.parametrize(
    "actual,predicted,matrix",
    [
        ([0, 0], [0, 0], [[2, 0], [0, 0]]),
        ([1, 1], [0, 0], [[0, 0], [2, 0]]),
        ([0, 0], [1, 1], [[0, 2], [0, 0]]),
    ],
)
def test_zero_denominators_are_defined_without_warnings(actual, predicted, matrix):
    result = evaluate_predictions(actual, predicted)
    assert result["precision"] == result["recall"] == result["f1_score"] == 0
    assert result["confusion_matrix"] == matrix


@pytest.mark.parametrize(
    "actual,predicted",
    [
        ([], []),
        ([0], [0, 1]),
        ([2], [0]),
        ([0], [-1]),
        ([np.nan], [0]),
        ([[0]], [[0]]),
    ],
)
def test_invalid_evaluation_input_is_rejected(actual, predicted):
    with pytest.raises(ValueError):
        evaluate_predictions(actual, predicted)


def test_json_and_csv_contain_matching_metrics(tmp_path):
    results = {"example": evaluate_predictions([0, 1, 1], [0, 1, 0])}
    json_path, csv_path = save_evaluation_reports(results, tmp_path)
    assert json.loads(json_path.read_text(encoding="utf-8")) == results
    row = pd.read_csv(csv_path).iloc[0]
    assert row["detector"] == "example"
    assert row["f1_score"] == pytest.approx(results["example"]["f1_score"])
    assert json.loads(row["confusion_matrix"]) == [[1, 0], [1, 1]]
