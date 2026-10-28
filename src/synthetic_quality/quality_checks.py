"""Non-mutating data contract checks with explicit failure-count semantics."""

from dataclasses import asdict, dataclass
from numbers import Real
from typing import Any

import numpy as np
import pandas as pd

from synthetic_quality.schema import (
    COUNTRY_CURRENCY,
    MERCHANT_CATEGORIES,
    REQUIRED_COLUMNS,
    STRING_COLUMNS,
    UNITS_PER_USD,
)


@dataclass(frozen=True)
class CheckResult:
    check_name: str
    failure_count: int
    details: dict[str, Any]
    skipped_columns: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.failure_count == 0


@dataclass(frozen=True)
class QualityReport:
    row_count: int
    checks: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_count": self.row_count,
            "passed": self.passed,
            "checks": [{**asdict(check), "passed": check.passed} for check in self.checks],
        }

    def raise_for_errors(self, dataset_name: str) -> None:
        if not self.passed:
            failed = ", ".join(
                f"{c.check_name}={c.failure_count}" for c in self.checks if not c.passed
            )
            raise ValueError(f"Data quality failed for {dataset_name}: {failed}")


def validate_transactions(frame: pd.DataFrame) -> QualityReport:
    """Inspect all rows; never coerce or drop data in the supplied frame.

    Missing/extra fields count columns, nulls/types count cells, duplicates count
    repeats after the first ID. Other checks count affected rows. Counts across
    checks can overlap; they must not be added to estimate invalid-row counts.
    Dependent checks explicitly list columns unavailable for inspection.
    """
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("Expected a pandas DataFrame")
    missing = sorted(set(REQUIRED_COLUMNS) - set(frame.columns))
    unexpected = sorted(set(frame.columns) - set(REQUIRED_COLUMNS), key=str)
    repeated_columns = int(frame.columns.duplicated().sum())
    # A repeated column name is itself invalid; inspect the first occurrence safely.
    data = frame.loc[:, ~frame.columns.duplicated()]
    checks = [
        CheckResult(
            "schema_mismatch",
            len(missing) + len(unexpected) + repeated_columns,
            {"missing": missing, "unexpected": unexpected, "repeated_columns": repeated_columns},
        ),
        CheckResult("missing_required_columns", len(missing), {"columns": missing}),
        CheckResult("empty_dataset", int(len(data) == 0), {}),
    ]
    nulls = {c: int(data[c].isna().sum()) for c in REQUIRED_COLUMNS if c in data}
    checks.append(CheckResult("null_values", sum(nulls.values()), nulls, tuple(missing)))

    duplicates = (
        int(data["transaction_id"].dropna().duplicated().sum()) if "transaction_id" in data else 0
    )
    checks.append(
        CheckResult(
            "duplicate_transaction_ids",
            duplicates,
            {},
            () if "transaction_id" in data else ("transaction_id",),
        )
    )
    amount_failures = 0
    if "transaction_amount" in data:
        amounts = pd.to_numeric(data["transaction_amount"], errors="coerce")
        bad_amounts = amounts.isna() | ~np.isfinite(amounts) | amounts.le(0)
        amount_failures = int((data["transaction_amount"].notna() & bad_amounts).sum())
    checks.append(
        CheckResult(
            "invalid_transaction_amounts",
            amount_failures,
            {},
            () if "transaction_amount" in data else ("transaction_amount",),
        )
    )
    type_failures = {}
    for column in REQUIRED_COLUMNS:
        if column not in data:
            continue
        non_null = data[column].dropna()
        if column in STRING_COLUMNS:
            bad = ~non_null.map(lambda value: isinstance(value, str)).astype(bool)
        elif column == "transaction_amount":
            bad = ~non_null.map(
                lambda value: isinstance(value, Real) and not isinstance(value, (bool, np.bool_))
            ).astype(bool)
        else:
            # The in-memory contract requires a native datetime column, not strings.
            bad = pd.Series(
                not pd.api.types.is_datetime64_any_dtype(data[column].dtype),
                index=non_null.index,
            )
        type_failures[column] = int(bad.sum())
    checks.append(
        CheckResult(
            "invalid_data_types", sum(type_failures.values()), type_failures, tuple(missing)
        )
    )
    unsupported = (
        int((data["currency"].notna() & ~data["currency"].isin(UNITS_PER_USD)).sum())
        if "currency" in data
        else 0
    )
    checks.append(
        CheckResult(
            "unsupported_currencies",
            unsupported,
            {},
            () if "currency" in data else ("currency",),
        )
    )
    violations = pd.Series(False, index=data.index)
    rule_counts = {}

    def add_rule(name: str, mask: pd.Series) -> None:
        nonlocal violations
        mask = mask.fillna(False).astype(bool)
        rule_counts[name] = int(mask.sum())
        violations |= mask

    for column in STRING_COLUMNS:
        if column in data:
            add_rule(
                f"blank_{column}",
                data[column].map(lambda v: isinstance(v, str) and not v.strip()),
            )
    if "merchant_category" in data:
        add_rule(
            "unsupported_merchant_category",
            data["merchant_category"].notna()
            & ~data["merchant_category"].isin(MERCHANT_CATEGORIES),
        )
    if "country" in data:
        add_rule(
            "unsupported_country", data["country"].notna() & ~data["country"].isin(COUNTRY_CURRENCY)
        )
    if {"country", "currency"}.issubset(data.columns):
        expected = data["country"].map(COUNTRY_CURRENCY)
        add_rule(
            "country_currency_mismatch",
            expected.notna() & data["currency"].notna() & data["currency"].ne(expected),
        )
    if "timestamp" in data and pd.api.types.is_datetime64_any_dtype(data["timestamp"].dtype):
        # UTC is the explicit toy-business clock; no wall-clock-dependent checks.
        add_rule(
            "timestamp_not_utc",
            pd.Series(str(data["timestamp"].dt.tz) != "UTC", index=data.index)
            & data["timestamp"].notna(),
        )
    checks.append(
        CheckResult("business_rule_violations", int(violations.sum()), rule_counts, tuple(missing))
    )
    return QualityReport(row_count=len(data), checks=tuple(checks))
