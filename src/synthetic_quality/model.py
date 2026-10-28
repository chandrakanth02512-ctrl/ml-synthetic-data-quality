"""Isolation Forest training, binary predictions, and trusted-local persistence."""

from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.pipeline import Pipeline
from sklearn.utils.validation import check_is_fitted

from synthetic_quality.features import build_preprocessor


@dataclass(frozen=True)
class ModelConfig:
    n_estimators: int = 200
    max_samples: int = 256
    contamination: float = 0.02
    random_state: int = 42

    def __post_init__(self) -> None:
        if isinstance(self.n_estimators, bool) or not isinstance(self.n_estimators, int):
            raise ValueError("n_estimators must be a positive integer")
        if isinstance(self.max_samples, bool) or not isinstance(self.max_samples, int):
            raise ValueError("max_samples must be a positive integer")
        if self.n_estimators < 1 or self.max_samples < 1:
            raise ValueError("n_estimators and max_samples must be positive")
        if not np.isfinite(self.contamination) or not 0 < self.contamination <= 0.5:
            raise ValueError("contamination must be in (0, 0.5]")


@dataclass(frozen=True)
class AnomalyPredictions:
    labels: np.ndarray
    scores: np.ndarray


def train_model(training: pd.DataFrame, config: ModelConfig | None = None) -> Pipeline:
    """Fit every learned component on training only; no label argument is accepted."""
    settings = config or ModelConfig()
    pipeline = Pipeline(
        [
            ("preprocess", build_preprocessor()),
            (
                "detector",
                IsolationForest(
                    n_estimators=settings.n_estimators,
                    max_samples=min(settings.max_samples, len(training)),
                    contamination=settings.contamination,
                    random_state=settings.random_state,
                    n_jobs=1,
                ),
            ),
        ]
    )
    pipeline.fit(training)
    return pipeline


def predict_anomalies(model: Pipeline, transactions: pd.DataFrame) -> AnomalyPredictions:
    # Negating the decision function makes larger scores more anomalous;
    # strictly positive scores cross the fitted training-only threshold.
    scores = -model.decision_function(transactions)
    labels = (model.predict(transactions) == -1).astype(np.int64)
    return AnomalyPredictions(labels=labels, scores=scores)


def save_model(model: Pipeline, path: str | Path) -> Path:
    check_is_fitted(model.named_steps["detector"])
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, destination, compress=3)
    return destination


def load_model(path: str | Path) -> Pipeline:
    """Load only trusted files: joblib/pickle can execute code on deserialization.

    Reuse the dependency versions recorded alongside the original model.
    """
    model = joblib.load(path)
    if not isinstance(model, Pipeline) or set(model.named_steps) != {"preprocess", "detector"}:
        raise ValueError("File does not contain a synthetic-quality model pipeline")
    if not isinstance(model.named_steps["detector"], IsolationForest):
        raise ValueError("Saved pipeline must contain an IsolationForest")
    check_is_fitted(model.named_steps["detector"])
    return model
