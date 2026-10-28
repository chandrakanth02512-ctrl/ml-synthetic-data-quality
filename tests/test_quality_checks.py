import numpy as np
import pandas as pd
import pytest

from synthetic_quality.quality_checks import validate_transactions


def checks(frame):
    return {check.check_name: check for check in validate_transactions(frame).checks}


def test_valid_data_passes_and_validation_does_not_mutate(normal_transactions):
    before = normal_transactions.copy(deep=True)
    report = validate_transactions(normal_transactions)
    assert report.passed
    assert all(c.failure_count == 0 for c in report.checks)
    assert report.to_dict()["passed"] is True
    pd.testing.assert_frame_equal(normal_transactions, before)


def test_missing_column_is_reported_and_dependent_checks_are_skipped(normal_transactions):
    result = checks(normal_transactions.drop(columns="transaction_amount"))
    assert result["missing_required_columns"].failure_count == 1
    assert result["schema_mismatch"].failure_count == 1
    assert result["invalid_transaction_amounts"].skipped_columns == ("transaction_amount",)


def test_extra_and_repeated_column_names_fail_schema(normal_transactions):
    assert checks(normal_transactions.assign(extra=1))["schema_mismatch"].failure_count == 1
    repeated = pd.concat([normal_transactions, normal_transactions[["currency"]]], axis=1)
    assert checks(repeated)["schema_mismatch"].failure_count == 1


def test_null_cells_are_counted(normal_transactions):
    normal_transactions.loc[0, "transaction_amount"] = np.nan
    normal_transactions.loc[1, "merchant_category"] = None
    normal_transactions.loc[2, "timestamp"] = pd.NaT
    result = checks(normal_transactions)
    assert result["null_values"].failure_count == 3
    assert result["invalid_transaction_amounts"].failure_count == 0


def test_duplicate_count_excludes_first_occurrence(normal_transactions):
    normal_transactions.loc[[1, 2], "transaction_id"] = normal_transactions.loc[0, "transaction_id"]
    assert checks(normal_transactions)["duplicate_transaction_ids"].failure_count == 2


@pytest.mark.parametrize("invalid", [-1, 0, np.inf, -np.inf, "not-a-number"])
def test_invalid_amounts(normal_transactions, invalid):
    normal_transactions["transaction_amount"] = normal_transactions["transaction_amount"].astype(
        object
    )
    normal_transactions.loc[0, "transaction_amount"] = invalid
    assert checks(normal_transactions)["invalid_transaction_amounts"].failure_count == 1


def test_unsupported_currency(normal_transactions):
    normal_transactions.loc[0, "currency"] = "XYZ"
    assert checks(normal_transactions)["unsupported_currencies"].failure_count == 1


@pytest.mark.parametrize(
    "column,value",
    [("transaction_amount", "23.50"), ("customer_id", 12), ("transaction_amount", True)],
)
def test_invalid_cell_types_are_not_silently_coerced(normal_transactions, column, value):
    normal_transactions[column] = normal_transactions[column].astype(object)
    normal_transactions.loc[0, column] = value
    report = checks(normal_transactions)
    assert report["invalid_data_types"].failure_count == 1
    assert normal_transactions.loc[0, column] == value


def test_string_timestamps_require_explicit_parsing(normal_transactions):
    normal_transactions["timestamp"] = normal_transactions["timestamp"].astype(str)
    assert checks(normal_transactions)["invalid_data_types"].failure_count == len(
        normal_transactions
    )


def test_business_rules_count_affected_rows_once(normal_transactions):
    normal_transactions.loc[0, "country"] = "US"
    normal_transactions.loc[0, "currency"] = "INR"
    normal_transactions.loc[1, "merchant_category"] = "unknown"
    normal_transactions.loc[1, "customer_id"] = " "
    normal_transactions.loc[2, "country"] = "ZZ"
    check = checks(normal_transactions)["business_rule_violations"]
    assert check.failure_count == 3
    assert check.details["country_currency_mismatch"] == 1
    assert check.details["blank_customer_id"] == 1


def test_timezone_naive_datetimes_fail_business_rule(normal_transactions):
    normal_transactions["timestamp"] = normal_transactions["timestamp"].dt.tz_localize(None)
    check = checks(normal_transactions)["business_rule_violations"]
    assert check.failure_count == len(normal_transactions)
    assert check.details["timestamp_not_utc"] == len(normal_transactions)


def test_empty_dataset_and_non_dataframe_are_rejected(normal_transactions):
    report = validate_transactions(normal_transactions.iloc[:0])
    assert not report.passed
    with pytest.raises(ValueError, match="empty_dataset=1"):
        report.raise_for_errors("empty")
    with pytest.raises(TypeError, match="DataFrame"):
        validate_transactions([])


def test_validation_error_names_failed_checks(normal_transactions):
    report = validate_transactions(normal_transactions.drop(columns="currency"))
    with pytest.raises(ValueError, match="missing_required_columns=1"):
        report.raise_for_errors("evaluation")


def test_all_null_column_is_reported_without_type_conversion_error(normal_transactions):
    normal_transactions["currency"] = None
    result = checks(normal_transactions)
    assert result["null_values"].failure_count == len(normal_transactions)
    assert result["invalid_data_types"].failure_count == 0
