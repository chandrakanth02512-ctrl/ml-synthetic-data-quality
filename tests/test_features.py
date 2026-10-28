import numpy as np
import pandas as pd
import pytest

from synthetic_quality.features import TransactionFeatures, build_preprocessor


def test_feature_matrix_is_finite_numeric_and_has_expected_dimensions(normal_transactions):
    preprocess = build_preprocessor()
    matrix = preprocess.fit_transform(normal_transactions)
    assert matrix.shape == (len(normal_transactions), 17)
    assert np.issubdtype(matrix.dtype, np.number)
    assert np.isfinite(matrix).all()
    assert len(preprocess.get_feature_names_out()) == matrix.shape[1]


def test_target_and_identifier_columns_cannot_enter_features(normal_transactions):
    preprocess = build_preprocessor().fit(normal_transactions)
    enriched = normal_transactions.assign(
        is_anomaly=1,
        injection_type="high_amount",
        injection_flag=True,
        transaction_id="leak",
        customer_id="leak",
        future_count=1_000,
    )
    np.testing.assert_array_equal(
        preprocess.transform(normal_transactions), preprocess.transform(enriched)
    )
    assert not any(
        "anomaly" in name or "injection" in name for name in preprocess.get_feature_names_out()
    )


def test_evaluation_does_not_refit_scaler_or_learn_new_categories(normal_transactions):
    preprocess = build_preprocessor().fit(normal_transactions)
    numeric = preprocess.named_steps["columns"].named_transformers_["numeric"]
    before = numeric.named_steps["scale"].mean_.copy()
    medians = numeric.named_steps["impute"].statistics_.copy()
    encoder = (
        preprocess.named_steps["columns"].named_transformers_["categorical"].named_steps["encode"]
    )
    categories = [values.copy() for values in encoder.categories_]
    evaluation = normal_transactions.assign(
        transaction_amount=1e9, merchant_category="new_merchant"
    )
    assert np.isfinite(preprocess.transform(evaluation)).all()
    np.testing.assert_array_equal(numeric.named_steps["scale"].mean_, before)
    np.testing.assert_array_equal(numeric.named_steps["impute"].statistics_, medians)
    for actual, expected in zip(encoder.categories_, categories, strict=True):
        np.testing.assert_array_equal(actual, expected)


def test_missing_columns_nulls_and_unknown_categories_transform_safely(normal_transactions):
    preprocess = build_preprocessor().fit(normal_transactions)
    incomplete = pd.DataFrame({"merchant_category": ["new", None], "currency": ["XYZ", None]})
    matrix = preprocess.transform(incomplete)
    assert matrix.shape == (2, 17)
    assert np.isfinite(matrix).all()
    empty_columns = pd.DataFrame(index=[0, 1])
    assert np.isfinite(preprocess.transform(empty_columns)).all()


def test_features_use_only_current_row_and_preserve_index(normal_transactions):
    extractor = TransactionFeatures()
    full = extractor.fit_transform(normal_transactions)
    row = extractor.transform(normal_transactions.iloc[[8]])
    pd.testing.assert_frame_equal(row, full.iloc[[8]])
    reordered = normal_transactions.sample(frac=1, random_state=7)
    pd.testing.assert_frame_equal(extractor.transform(reordered).sort_index(), full)


def test_currency_normalization_and_cyclic_hour_values(normal_transactions):
    subset = normal_transactions.iloc[:2].copy()
    subset["currency"] = ["USD", "INR"]
    subset["transaction_amount"] = [100.0, 8_000.0]
    subset["timestamp"] = pd.to_datetime(["2025-01-01T00:00Z", "2025-01-01T06:00Z"], utc=True)
    features = TransactionFeatures().transform(subset)
    np.testing.assert_allclose(features["log_amount_usd"], np.log1p(100.0))
    np.testing.assert_allclose(features["hour_sin"], [0, 1], atol=1e-12)
    np.testing.assert_allclose(features["hour_cos"], [1, 0], atol=1e-12)


def test_non_dataframe_input_rejected():
    with pytest.raises(TypeError, match="DataFrame"):
        TransactionFeatures().fit([[1]])
    with pytest.raises(TypeError, match="DataFrame"):
        TransactionFeatures().transform([[1]])
