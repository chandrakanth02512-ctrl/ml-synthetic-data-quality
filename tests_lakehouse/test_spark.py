import hashlib
import io
import json

import pandas as pd
import pytest

from synthetic_quality.quality_checks import validate_transactions
from synthetic_quality_lakehouse.source import parse_source
from synthetic_quality_lakehouse.spark import CHECKS, transform


def process(spark, data):
    rows = parse_source(data, hashlib.sha256(data).hexdigest())
    return transform(spark, rows, "pilot-001", "f" * 64)


def test_real_spark_agrees_with_existing_pandas_contract(spark, normal_data):
    frame = pd.read_json(io.StringIO(normal_data.decode()), lines=True)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    assert validate_transactions(frame).passed
    out = process(spark, normal_data)
    try:
        clean = out.clean.collect()
        assert len(clean) == 16 and out.quarantine.count() == 0
        assert all(r.failed_rows == 0 and r.passed for r in out.quality.collect())
        rates = {"USD": 1, "GBP": 0.8, "EUR": 0.9, "INR": 80}
        for r in clean:
            assert r.transaction_amount_usd == pytest.approx(
                r.transaction_amount / rates[r.currency]
            )
            assert r.transaction_date == r.timestamp.date()
    finally:
        out.classified.unpersist()


def test_quarantine_captures_all_duplicates_bad_schema_types_and_business_rules(spark, normal_data):
    rows = [json.loads(r) for r in normal_data.splitlines()]
    rows.append(dict(rows[0]))  # Quarantine BOTH members; never choose an arbitrary winner.
    changes = {
        1: {"customer_id": None},
        2: {"transaction_amount": -1},
        3: {"timestamp": "not-a-time"},
        4: {"merchant_category": "unknown"},
        5: {"country": "US", "currency": "INR"},
        6: {"currency": "XYZ"},
        7: {"customer_id": " "},
        8: {"transaction_amount": "10"},
        9: {"timestamp": "2025-01-01T10:00:00+02:00"},
        10: {"extra": "x"},
        11: {"country": "XX"},
    }
    for i, values in changes.items():
        rows[i].update(values)
    del rows[12]["currency"]
    data = ("\n".join(json.dumps(r) for r in rows) + "\nnot-json\n").encode()
    out = process(spark, data)
    try:
        bad = {r.source_row_number: r for r in out.quarantine.collect()}
        expected = {
            1: "DUPLICATE_TRANSACTION_ID",
            17: "DUPLICATE_TRANSACTION_ID",
            2: "NULL_REQUIRED",
            3: "INVALID_AMOUNT",
            4: "INVALID_TIMESTAMP",
            5: "UNSUPPORTED_CATEGORY",
            6: "COUNTRY_CURRENCY_MISMATCH",
            7: "UNSUPPORTED_CURRENCY",
            8: "BLANK_REQUIRED",
            9: "INVALID_TYPES",
            10: "INVALID_TIMESTAMP",
            11: "SCHEMA_MISMATCH",
            12: "UNSUPPORTED_COUNTRY",
            13: "SCHEMA_MISMATCH",
            18: "MALFORMED_JSON",
        }
        assert set(bad) == set(expected)
        for n, code in expected.items():
            assert code in bad[n].error_codes
            assert bad[n].raw_payload == data.decode().splitlines()[n - 1]
        assert out.clean.count() + len(bad) == 18
        assert out.ledger.count() == 18
        quality = {r.check_name: r for r in out.quality.collect()}
        assert set(quality) == set(CHECKS)
        assert quality["DUPLICATE_TRANSACTION_ID"].failed_rows == 2
        assert quality["MALFORMED_JSON"].failed_rows == 1
        assert all(r.evaluated_rows == 18 for r in quality.values())
    finally:
        out.classified.unpersist()


def test_nonfinite_and_bad_calendar_timestamps_are_quarantined(spark, normal_data):
    row = json.loads(normal_data.splitlines()[0])
    row["transaction_amount"] = 1e309
    data = json.dumps(row).encode()
    out = process(spark, data)
    try:
        assert "MALFORMED_JSON" in out.quarantine.first().error_codes
    finally:
        out.classified.unpersist()


@pytest.mark.parametrize("data", [b"", b"\xff", b"{}\n{}"])
def test_invalid_source_fails_before_spark(data):
    with pytest.raises((ValueError, UnicodeError)):
        parse_source(data, hashlib.sha256(data).hexdigest(), max_rows=1)


@pytest.mark.parametrize("payload", [b"[]", b'{"x":1,"x":2}', b'{"x":NaN}'])
def test_malformed_input_preserved_for_quarantine(payload):
    row = parse_source(payload, hashlib.sha256(payload).hexdigest())[0]
    assert row["parser_errors"] == ["MALFORMED_JSON"]
    assert row["raw_payload"].encode() == payload


def test_wrong_source_hash_is_rejected(normal_data):
    with pytest.raises(ValueError, match="SHA-256"):
        parse_source(normal_data, "0" * 64)


@pytest.mark.parametrize(
    "updates,code",
    [
        ({"customer_id": "\t\n\u00a0"}, "BLANK_REQUIRED"),
        ({"timestamp": "2025-02-31T10:00:00Z"}, "INVALID_TIMESTAMP"),
        ({"transaction_amount": 1.7e308, "country": "GB", "currency": "GBP"}, "INVALID_AMOUNT"),
    ],
)
def test_contract_edge_cases_do_not_enter_clean_table(spark, normal_data, updates, code):
    row = json.loads(normal_data.splitlines()[0])
    row.update(updates)
    out = process(spark, json.dumps(row).encode())
    try:
        assert out.clean.count() == 0
        assert code in out.quarantine.first().error_codes
    finally:
        out.classified.unpersist()
