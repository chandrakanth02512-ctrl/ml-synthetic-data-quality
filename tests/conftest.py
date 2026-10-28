import pytest

from synthetic_quality.data_generator import generate_transactions
from synthetic_quality.model import ModelConfig, train_model


@pytest.fixture
def normal_transactions():
    return generate_transactions(300, seed=42, dataset_name="training").transactions


@pytest.fixture
def evaluation_dataset():
    return generate_transactions(
        200,
        seed=123,
        anomaly_fraction=0.05,
        dataset_name="evaluation",
        start_date="2025-02-01",
    )


@pytest.fixture
def model_config():
    return ModelConfig(n_estimators=40, max_samples=128)


@pytest.fixture
def fitted_model(normal_transactions, model_config):
    return train_model(normal_transactions, model_config)
