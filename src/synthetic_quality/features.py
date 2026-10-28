"""Allowlisted, row-local features and training-fitted preprocessing."""

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from synthetic_quality.schema import UNITS_PER_USD

NUMERIC_FEATURES = ("log_amount_usd", "hour_sin", "hour_cos")
CATEGORICAL_FEATURES = ("merchant_category", "currency", "country")


class TransactionFeatures(TransformerMixin, BaseEstimator):
    """Ignore IDs, truth, flags, and every other non-allowlisted input field.

    Only the current row is used: predictions are independent of batch ordering
    and future records. Missing raw inputs become NaN for downstream imputation.
    Strict data quality validation is still required before pipeline training.
    """

    def fit(self, X: pd.DataFrame, y=None):
        if not isinstance(X, pd.DataFrame):
            raise TypeError("TransactionFeatures expects a pandas DataFrame")
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(X, pd.DataFrame):
            raise TypeError("TransactionFeatures expects a pandas DataFrame")

        def column(name: str) -> pd.Series:
            return X[name] if name in X else pd.Series(np.nan, index=X.index)

        amounts = pd.to_numeric(column("transaction_amount"), errors="coerce")
        rates = column("currency").map(UNITS_PER_USD)
        normalized = amounts / rates
        normalized = normalized.where(np.isfinite(normalized) & normalized.gt(0))
        timestamps = pd.to_datetime(column("timestamp"), errors="coerce", utc=True)
        fractional_hour = (
            timestamps.dt.hour + timestamps.dt.minute / 60 + timestamps.dt.second / 3600
        )
        angle = 2 * np.pi * fractional_hour / 24
        result = pd.DataFrame(
            {
                "log_amount_usd": np.log1p(normalized),
                "hour_sin": np.sin(angle),
                "hour_cos": np.cos(angle),
            },
            index=X.index,
        )
        for name in CATEGORICAL_FEATURES:
            # Uniform string representation avoids mixed-category encoder failures.
            result[name] = column(name).map(lambda v: str(v) if pd.notna(v) else np.nan)
        return result

    def get_feature_names_out(self, input_features=None) -> np.ndarray:
        return np.asarray(NUMERIC_FEATURES + CATEGORICAL_FEATURES, dtype=object)


def build_preprocessor() -> Pipeline:
    numeric = Pipeline(
        [
            ("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
            ("scale", StandardScaler()),
        ]
    )
    categorical = Pipeline(
        [
            (
                "impute",
                SimpleImputer(strategy="constant", fill_value="missing", keep_empty_features=True),
            ),
            (
                "encode",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False, dtype=np.float64),
            ),
        ]
    )
    columns = ColumnTransformer(
        [
            ("numeric", numeric, list(NUMERIC_FEATURES)),
            ("categorical", categorical, list(CATEGORICAL_FEATURES)),
        ],
        remainder="drop",
        verbose_feature_names_out=True,
    )
    return Pipeline([("features", TransactionFeatures()), ("columns", columns)])
