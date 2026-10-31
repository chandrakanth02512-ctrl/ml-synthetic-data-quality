"""Explicit BigQuery contracts; no autodetection or schema evolution."""

META = [
    ("run_id", "STRING"),
    ("source_fingerprint", "STRING"),
    ("source_row_id", "STRING"),
    ("source_row_number", "INT64"),
    ("source_record_sha256", "STRING"),
]
DEFINITIONS = {
    "clean": META
    + [
        ("transaction_id", "STRING"),
        ("customer_id", "STRING"),
        ("timestamp", "TIMESTAMP"),
        ("transaction_amount", "FLOAT64"),
        ("merchant_category", "STRING"),
        ("currency", "STRING"),
        ("country", "STRING"),
        ("transaction_amount_usd", "FLOAT64"),
        ("transaction_date", "DATE"),
    ],
    "quarantine": META + [("raw_payload", "STRING"), ("error_codes", "STRING", "REPEATED")],
    "quality": [
        ("run_id", "STRING"),
        ("source_fingerprint", "STRING"),
        ("check_name", "STRING"),
        ("evaluated_rows", "INT64"),
        ("failed_rows", "INT64"),
        ("passed", "BOOL"),
    ],
    "ledger": META + [("disposition", "STRING"), ("amount_usd", "FLOAT64", "NULLABLE")],
    "runs": [
        ("run_id", "STRING"),
        ("source_fingerprint", "STRING"),
        ("source_rows", "INT64"),
        ("clean_rows", "INT64"),
        ("quarantined_rows", "INT64"),
        ("quality_rows", "INT64"),
        ("source_uri", "STRING"),
        ("source_generation", "INT64"),
        ("source_sha256", "STRING"),
        ("committed_at", "TIMESTAMP"),
    ],
}
TABLES = {
    "clean": "clean_transactions",
    "quarantine": "quarantined_records",
    "quality": "quality_results",
    "runs": "pipeline_runs",
}


def schema_json(kind):
    return [
        {"name": f[0], "type": f[1], "mode": f[2] if len(f) > 2 else "REQUIRED"}
        for f in DEFINITIONS[kind]
    ]


def ddl(dataset):
    from synthetic_quality_lakehouse.config import dataset_name

    project, name = dataset.split(".")
    dataset_name(project, name)
    statements = []
    for kind, table in TABLES.items():
        columns = []
        for f in schema_json(kind):
            field_type = f"ARRAY<{f['type']}>" if f["mode"] == "REPEATED" else f["type"]
            required = " NOT NULL" if f["mode"] == "REQUIRED" else ""
            columns.append(f"`{f['name']}` {field_type}{required}")
        statements.append(
            f"CREATE TABLE IF NOT EXISTS `{dataset}.{table}` (\n  "
            + ",\n  ".join(columns)
            + "\n) OPTIONS (labels=["
            + "('application', 'synthetic-quality-lakehouse')]);"
        )
    return "\n\n".join(statements)
