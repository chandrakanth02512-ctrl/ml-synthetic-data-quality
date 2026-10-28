"""A simple, explicit rule baseline calibrated solely on normal training amounts."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from synthetic_quality.schema import UNITS_PER_USD


def amounts_usd(transactions: pd.DataFrame) -> pd.Series:
    return transactions["transaction_amount"] / transactions["currency"].map(UNITS_PER_USD)


@dataclass(frozen=True)
class RuleBaseline:
    amount_threshold_usd: float
    amount_quantile: float = 0.995
    early_hour_cutoff: int = 6

    @classmethod
    def fit(cls, training: pd.DataFrame, amount_quantile: float = 0.995) -> "RuleBaseline":
        if not np.isfinite(amount_quantile) or not 0 < amount_quantile < 1:
            raise ValueError("amount_quantile must be in (0, 1)")
        return cls(
            amount_threshold_usd=float(amounts_usd(training).quantile(amount_quantile)),
            amount_quantile=amount_quantile,
        )

    def predict(self, transactions: pd.DataFrame) -> np.ndarray:
        """Flag large amounts OR UTC timestamps before 06:00."""
        large_amount = amounts_usd(transactions) > self.amount_threshold_usd
        early_hour = transactions["timestamp"].dt.hour < self.early_hour_cutoff
        return (large_amount | early_hour).to_numpy(dtype=np.int64)
