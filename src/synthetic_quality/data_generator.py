"""Generate independent transactions and a separate, explicitly labeled truth table."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from synthetic_quality.schema import COUNTRY_CURRENCY, MERCHANT_CATEGORIES, UNITS_PER_USD


@dataclass(frozen=True)
class GeneratedDataset:
    transactions: pd.DataFrame
    truth: pd.DataFrame


def generate_transactions(
    n_rows: int,
    seed: int,
    anomaly_fraction: float = 0.0,
    *,
    dataset_name: str = "sample",
    start_date: str = "2025-01-01",
) -> GeneratedDataset:
    """Generate a 28-day UTC batch; round(n_rows * fraction) rows are injected.

    Normal amounts are lognormal in USD-equivalent units, then converted to
    nominal currency. Normal timestamps fall between 07:00 and 22:59 UTC.
    Injection types alternate over randomly selected rows for balanced coverage.
    IDs include the split and seed; all random state is local to this function.
    """
    if isinstance(n_rows, bool) or not isinstance(n_rows, int) or n_rows <= 0:
        raise ValueError("n_rows must be a positive integer")
    if not np.isfinite(anomaly_fraction) or not 0 <= anomaly_fraction <= 1:
        raise ValueError("anomaly_fraction must be finite and between 0 and 1")
    if not dataset_name or not dataset_name.replace("_", "").isalnum():
        raise ValueError("dataset_name must contain only letters, numbers, or underscores")
    start = pd.Timestamp(start_date)
    if pd.isna(start):
        raise ValueError("start_date must be a valid date")
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    start = start.normalize()
    rng = np.random.default_rng(seed)
    countries = rng.choice(list(COUNTRY_CURRENCY), size=n_rows, p=[0.5, 0.2, 0.2, 0.1])
    currencies = np.array([COUNTRY_CURRENCY[c] for c in countries])
    currency_rates = np.array([UNITS_PER_USD[c] for c in currencies])
    amounts = np.maximum(rng.lognormal(mean=3.7, sigma=0.8, size=n_rows), 1.0)
    hours = rng.choice(np.arange(7, 23), size=n_rows)
    days = rng.integers(0, 28, size=n_rows)
    minutes = rng.integers(0, 60, size=n_rows)
    seconds = rng.integers(0, 60, size=n_rows)
    categories = rng.choice(MERCHANT_CATEGORIES, size=n_rows)
    customers = rng.integers(1, 501, size=n_rows)

    n_anomalies = round(n_rows * anomaly_fraction)
    selected = rng.choice(n_rows, size=n_anomalies, replace=False)
    high_amount = selected[::2]
    unusual_time = selected[1::2]
    amounts[high_amount] = rng.uniform(2_000, 5_000, size=len(high_amount))
    hours[unusual_time] = rng.integers(0, 5, size=len(unusual_time))
    injection_types = np.full(n_rows, "normal", dtype=object)
    injection_types[high_amount] = "high_amount"
    injection_types[unusual_time] = "unusual_time"
    timestamps = (
        start
        + pd.to_timedelta(days, unit="D")
        + pd.to_timedelta(hours, unit="h")
        + pd.to_timedelta(minutes, unit="m")
        + pd.to_timedelta(seconds, unit="s")
    )
    transaction_ids = [f"{dataset_name}_{seed}_{i:07d}" for i in range(n_rows)]
    transactions = pd.DataFrame(
        {
            "transaction_id": transaction_ids,
            "customer_id": [f"{dataset_name}_C{c:04d}" for c in customers],
            "timestamp": timestamps,
            "transaction_amount": np.round(amounts * currency_rates, 2),
            "merchant_category": categories,
            "currency": currencies,
            "country": countries,
        }
    )
    truth = pd.DataFrame(
        {
            "transaction_id": transaction_ids,
            "is_anomaly": (injection_types != "normal").astype(np.int64),
            "injection_type": injection_types,
        }
    )
    return GeneratedDataset(transactions=transactions, truth=truth)
