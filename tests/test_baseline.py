import numpy as np
import pandas as pd
import pytest

from synthetic_quality.baseline import RuleBaseline


def test_threshold_uses_training_quantile_and_converts_currency():
    training = pd.DataFrame({"transaction_amount": [10, 20, 30, 40], "currency": ["USD"] * 4})
    baseline = RuleBaseline.fit(training, amount_quantile=0.75)
    assert baseline.amount_threshold_usd == 32.5
    evaluation = pd.DataFrame(
        {
            "transaction_amount": [32.5, 33, 8, 8_000],
            "currency": ["USD", "USD", "USD", "INR"],
            "timestamp": pd.to_datetime(
                [
                    "2025-02-01T12:00Z",
                    "2025-02-01T12:00Z",
                    "2025-02-01T02:00Z",
                    "2025-02-01T12:00Z",
                ],
                utc=True,
            ),
        }
    )
    np.testing.assert_array_equal(baseline.predict(evaluation), [0, 1, 1, 1])
    assert baseline.amount_threshold_usd == 32.5


@pytest.mark.parametrize("quantile", [0, 1, -0.5, float("nan")])
def test_invalid_quantile(normal_transactions, quantile):
    with pytest.raises(ValueError, match="amount_quantile"):
        RuleBaseline.fit(normal_transactions, quantile)
