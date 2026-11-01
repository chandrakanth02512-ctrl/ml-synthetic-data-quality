"""Immutable staging loads and atomic, run-scoped BigQuery publication.

No automatic mutation retry. An ambiguous response is recovered by its fixed job ID.
No dataset creation, deletion, arbitrary SQL or target overwrite occurs here.
"""

import argparse
import hashlib
import json
from importlib.resources import files

from google.api_core.exceptions import Conflict, NotFound
from google.cloud import bigquery as bq

from synthetic_quality_lakehouse.config import canonical, dataset_name
from synthetic_quality_lakehouse.manifest import validate_manifest
from synthetic_quality_lakehouse.schemas import TABLES, ddl, schema_json

LABELS = {"application": "synthetic-quality-lakehouse"}


def schema(kind):
    return [bq.SchemaField.from_api_repr(f) for f in schema_json(kind)]


def validate_table(table, kind, location=None):
    expected = [(f.name, f.field_type, f.mode) for f in schema(kind)]
    actual = [(f.name, f.field_type, f.mode) for f in table.schema]
    # SDK may normalize SQL type aliases.
    aliases = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}

    def normalize(xs):
        return [(n, aliases.get(t, t), m) for n, t, m in xs]

    if normalize(actual) != normalize(expected) or table.labels != LABELS:
        raise ValueError("Existing table schema/ownership differs; no alteration authorized")
    if location and getattr(table, "location", None) not in (None, location):
        raise ValueError("Table location differs")


def sql_text(name):
    return files("synthetic_quality_lakehouse").joinpath("sql", name).read_text()


def render_sql(dataset, stages):
    project, name = dataset.split(".")
    dataset_name(project, name)
    from synthetic_quality_lakehouse.config import identifier

    for value in stages.values():
        if value.rsplit(".", 1)[0] != dataset:
            raise ValueError("Stage outside dedicated dataset")
        identifier(value.rsplit(".", 1)[1])
    reconciliation = sql_text("reconcile.sql").format(dataset=dataset, ledger=stages["ledger"])
    assertion = (
        "SELECT source_rows = @source_rows AND target_rows = @source_rows "
        "AND source_duplicates = 0 AND target_duplicates = 0 AND mismatched_rows = 0 "
        "FROM (" + reconciliation + ")"
    )
    return sql_text("publish.sql").format(dataset=dataset, **stages, reconcile=assertion)


def job_once(client, job_id, location, expected_configuration, submit):
    try:
        job = client.get_job(job_id, location=location, retry=None)
    except NotFound:
        try:
            job = submit()
        except Conflict:
            job = client.get_job(job_id, location=location, retry=None)
    actual = job.to_api_repr().get("configuration", {})
    for kind, expected in expected_configuration.items():
        if kind == "labels":
            continue
        value = actual.get(kind, {})

        def comparable(key, data):
            if key == "schema" and isinstance(data, dict):
                aliases = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}
                return {
                    **data,
                    "fields": [
                        {**f, "type": aliases.get(f["type"], f["type"])}
                        for f in data.get("fields", [])
                    ],
                }
            return data

        if any(comparable(k, value.get(k)) != comparable(k, v) for k, v in expected.items()):
            raise ValueError("Existing job ID has different configuration; no reuse")
    # Failed jobs are never replaced/retried. Polling is read-only; no job_retry resubmission.
    job.result(retry=None)
    return job


def load(manifest, store, client, project, dataset, location="asia-south1"):
    config = validate_manifest(manifest)
    ds = dataset_name(project, dataset)
    if client.get_dataset(ds, retry=None).location.lower() != location.lower():
        raise ValueError("BigQuery dataset must share the reviewed regional location")
    for kind, name in TABLES.items():
        validate_table(client.get_table(ds + "." + name, retry=None), kind)
    # Verify every immutable GCS object before loading, and after each load's acknowledgement.
    for record in manifest["artifacts"].values():
        store.verify(record)
    token = hashlib.sha256(
        canonical(
            {
                "run": config.run_id,
                "fingerprint": manifest["fingerprint"],
                "dataset": ds,
                "location": location,
            }
        )
    ).hexdigest()[:32]
    stages = {}
    for kind, record in manifest["artifacts"].items():
        table_id = ds + ".stg_" + kind + "_" + token
        stages[kind] = table_id
        table = bq.Table(table_id, schema=schema(kind))
        table.labels = LABELS
        try:
            existing = client.get_table(table_id, retry=None)
        except NotFound:
            try:
                existing = client.create_table(table, retry=None)
            except Conflict:
                existing = client.get_table(table_id, retry=None)
        validate_table(existing, kind)
        if not record["row_count"]:
            if existing.num_rows != 0:
                raise ValueError("Unexpected rows in empty stage")
            continue
        job_config = bq.LoadJobConfig(
            schema=schema(kind),
            source_format=bq.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bq.WriteDisposition.WRITE_EMPTY,
            create_disposition=bq.CreateDisposition.CREATE_NEVER,
            max_bad_records=0,
            ignore_unknown_values=False,
            labels=LABELS,
        )
        expected = job_config.to_api_repr()
        expected["load"].update(
            {
                "sourceUris": [record["uri"]],
                "destinationTable": bq.TableReference.from_string(table_id).to_api_repr(),
            }
        )
        job_once(
            client,
            "idq_" + token + "_" + kind,
            location,
            expected,
            lambda record=record, table_id=table_id, job_config=job_config, kind=kind: (
                client.load_table_from_uri(
                    record["uri"],
                    table_id,
                    job_config=job_config,
                    job_id="idq_" + token + "_" + kind,
                    location=location,
                    retry=None,
                )
            ),
        )
        store.verify(record)
    params = {
        "run_id": ("STRING", config.run_id),
        "fingerprint": ("STRING", manifest["fingerprint"]),
        "source_rows": ("INT64", manifest["source_rows"]),
        "clean_rows": ("INT64", manifest["artifacts"]["clean"]["row_count"]),
        "quarantined_rows": ("INT64", manifest["artifacts"]["quarantine"]["row_count"]),
        "quality_rows": ("INT64", manifest["artifacts"]["quality"]["row_count"]),
        "source_uri": ("STRING", config.source_uri),
        "source_generation": ("INT64", config.source_generation),
        "source_sha256": ("STRING", config.source_sha256),
    }
    query = render_sql(ds, stages)
    job_config = bq.QueryJobConfig(
        use_legacy_sql=False,
        maximum_bytes_billed=100_000_000,
        labels=LABELS,
        query_parameters=[bq.ScalarQueryParameter(k, *v) for k, v in params.items()],
    )
    expected = job_config.to_api_repr()
    expected["query"]["query"] = query
    job_id = "idq_" + token + "_publish"
    job = job_once(
        client,
        job_id,
        location,
        expected,
        lambda: client.query(
            query,
            job_config=job_config,
            job_id=job_id,
            location=location,
            retry=None,
            job_retry=None,
        ),
    )
    return {
        "status": "PUBLISHED",
        "run_id": config.run_id,
        "job_id": job.job_id,
        "fingerprint": manifest["fingerprint"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("ddl", "load"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--dataset", default="synthetic_quality_lakehouse")
    parser.add_argument("--location", default="asia-south1")
    parser.add_argument("--manifest-uri")
    parser.add_argument("--approve-publication", action="store_true")
    args = parser.parse_args()
    ds = dataset_name(args.project, args.dataset)
    if args.action == "ddl":
        print(ddl(ds))  # Rendering only, never executes DDL.
        return
    if not args.approve_publication or not args.manifest_uri:
        parser.error(
            "Load requires a separately approved publication and exact completion manifest"
        )
    from google.cloud import storage

    from synthetic_quality_lakehouse.storage import GCSObjects

    store = GCSObjects(storage.Client(project=args.project))
    manifest = json.loads(store.read(args.manifest_uri)[0])
    config = validate_manifest(manifest)
    if args.manifest_uri != config.root + "/completion.json":
        raise ValueError("Unexpected manifest path")
    print(
        json.dumps(
            load(
                manifest,
                store,
                bq.Client(project=args.project),
                args.project,
                args.dataset,
                args.location,
            )
        )
    )


if __name__ == "__main__":
    main()
