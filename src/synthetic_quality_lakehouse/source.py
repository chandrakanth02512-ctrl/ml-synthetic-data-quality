"""Bounded NDJSON transport parsing; business validation remains in Spark SQL."""

import hashlib
import json

from synthetic_quality.schema import REQUIRED_COLUMNS


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError("Nonstandard JSON number")


def parse_source(data, expected_sha256, max_rows=100_000):
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("Source SHA-256 differs from reviewed immutable input")
    text = data.decode("utf-8", errors="strict")
    lines = text.splitlines()
    if not lines or len(lines) > max_rows:
        raise ValueError("Source empty or exceeds pilot row bound")
    records = []
    for number, line in enumerate(lines, 1):
        errors, values = [], {}
        try:
            obj = json.loads(
                line, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
            )
            if not isinstance(obj, dict):
                raise ValueError("Expected JSON object")
            if set(obj) != set(REQUIRED_COLUMNS):
                errors.append("SCHEMA_MISMATCH")
            for column in REQUIRED_COLUMNS:
                value = obj.get(column)
                valid_type = (
                    isinstance(value, (int, float)) and not isinstance(value, bool)
                    if column == "transaction_amount"
                    else isinstance(value, str)
                )
                if value is not None and not valid_type:
                    errors.append("INVALID_TYPES")
                values[column] = str(value) if value is not None and valid_type else None
        except (ValueError, TypeError):
            errors.append("MALFORMED_JSON")
        records.append(
            {
                **{c: values.get(c) for c in REQUIRED_COLUMNS},
                "source_row_number": number,
                "source_row_id": hashlib.sha256(f"{expected_sha256}:{number}".encode()).hexdigest(),
                "source_record_sha256": hashlib.sha256(line.encode()).hexdigest(),
                "raw_payload": line,
                "parser_errors": sorted(set(errors)),
            }
        )
    return records
