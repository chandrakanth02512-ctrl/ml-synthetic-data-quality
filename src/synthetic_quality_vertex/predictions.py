"""Replay-safe prediction loading, reusing PR #4's fixed-job reconciliation."""

import argparse
import json

from google.api_core.exceptions import Conflict, NotFound
from google.cloud import bigquery as bq

from synthetic_quality_lakehouse.bigquery import PUBLICATION_MAX_BYTES_BILLED, job_once
from synthetic_quality_lakehouse.config import dataset_name
from synthetic_quality_vertex.config import fingerprint
from synthetic_quality_vertex.prediction_sql import PUBLISH
from synthetic_quality_vertex.train import validate_completion

LABELS = {"application": "synthetic-quality-vertex"}
PREDICTIONS = [
    ("run_id", "STRING"),
    ("source_fingerprint", "STRING"),
    ("transaction_id", "STRING"),
    ("source_row_id", "STRING"),
    ("source_record_sha256", "STRING"),
    ("anomaly_score", "FLOAT64"),
    ("rule_based_prediction", "INT64"),
    ("isolation_forest_prediction", "INT64"),
    ("combined_or_prediction", "INT64"),
    ("actual_anomaly", "INT64"),
    ("model_sha256", "STRING"),
]
RUNS = [
    ("run_id", "STRING"),
    ("source_fingerprint", "STRING"),
    ("model_sha256", "STRING"),
    ("training_rows", "INT64"),
    ("evaluation_rows", "INT64"),
    ("committed_at", "TIMESTAMP"),
]


def schema(fields):
    return [bq.SchemaField(n, t, mode="REQUIRED") for n, t in fields]


def validate_table(table, fields):
    aliases = {"INTEGER": "INT64", "FLOAT": "FLOAT64"}
    actual = [(f.name, aliases.get(f.field_type, f.field_type), f.mode) for f in table.schema]
    if actual != [(n, t, "REQUIRED") for n, t in fields] or table.labels != LABELS:
        raise ValueError("Prediction table schema/ownership differs; no repair")


def ddl(project, dataset):
    ds = dataset_name(project, dataset)
    return "\n".join(
        f"CREATE TABLE IF NOT EXISTS `{ds}.{name}` ("
        + ", ".join(f"`{n}` {t} NOT NULL" for n, t in fields)
        + ") OPTIONS (labels=[('application','synthetic-quality-vertex')]);"
        for name, fields in (("anomaly_predictions", PREDICTIONS), ("model_runs", RUNS))
    )


def load(manifest, store, client, project, dataset, location="asia-south1"):
    config = validate_completion(manifest)
    ds = dataset_name(project, dataset)
    if client.get_dataset(ds, retry=None).location.lower() != location.lower():
        raise ValueError("Prediction dataset location differs")
    for name, fields in (("anomaly_predictions", PREDICTIONS), ("model_runs", RUNS)):
        validate_table(client.get_table(ds + "." + name, retry=None), fields)
    for record in manifest["artifacts"].values():
        store.verify(record)
    token = fingerprint(
        {
            "run": config.run_id,
            "fingerprint": manifest["fingerprint"],
            "dataset": ds,
            "location": location,
        }
    )[:32]
    stage = ds + ".stg_predictions_" + token
    try:
        table = client.get_table(stage, retry=None)
    except NotFound:
        table = bq.Table(stage, schema=schema(PREDICTIONS))
        table.labels = LABELS
        try:
            table = client.create_table(table, retry=None)
        except Conflict:
            table = client.get_table(stage, retry=None)
    validate_table(table, PREDICTIONS)
    record = manifest["artifacts"]["predictions"]
    job_config = bq.LoadJobConfig(
        schema=schema(PREDICTIONS),
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
            "destinationTable": bq.TableReference.from_string(stage).to_api_repr(),
        }
    )
    job_id = "vertex_" + token + "_load"
    job_once(
        client,
        job_id,
        location,
        expected,
        lambda: client.load_table_from_uri(
            record["uri"],
            stage,
            job_config=job_config,
            job_id=job_id,
            location=location,
            retry=None,
        ),
    )
    store.verify(record)
    params = {
        "run_id": ("STRING", config.run_id),
        "fingerprint": ("STRING", manifest["fingerprint"]),
        "model_sha256": ("STRING", manifest["artifacts"]["model"]["sha256"]),
        "training_rows": ("INT64", manifest["training_rows"]),
        "evaluation_rows": ("INT64", manifest["evaluation_rows"]),
    }
    query = PUBLISH.format(dataset=ds, stage=stage)
    query_config = bq.QueryJobConfig(
        use_legacy_sql=False,
        maximum_bytes_billed=PUBLICATION_MAX_BYTES_BILLED,
        labels=LABELS,
        query_parameters=[bq.ScalarQueryParameter(k, *v) for k, v in params.items()],
    )
    expected = query_config.to_api_repr()
    expected["query"]["query"] = query
    query_id = "vertex_" + token + "_publish"
    job_once(
        client,
        query_id,
        location,
        expected,
        lambda: client.query(
            query,
            job_config=query_config,
            job_id=query_id,
            location=location,
            retry=None,
            job_retry=None,
        ),
    )
    return {"status": "PUBLISHED", "run_id": config.run_id, "job_id": query_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("ddl", "load"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--dataset", default="synthetic_quality_lakehouse")
    parser.add_argument("--manifest-uri")
    parser.add_argument("--approve-publication", action="store_true")
    args = parser.parse_args()
    if args.action == "ddl":
        print(ddl(args.project, args.dataset))
        return
    if not args.approve_publication or not args.manifest_uri:
        parser.error("Separate prediction-publication approval and manifest required")
    from google.cloud import storage

    from synthetic_quality_lakehouse.storage import GCSObjects

    store = GCSObjects(storage.Client(project=args.project))
    manifest = json.loads(store.read(args.manifest_uri)[0])
    if args.manifest_uri != validate_completion(manifest).root + "/completion.json":
        raise ValueError("Unexpected Vertex completion path")
    print(
        json.dumps(
            load(manifest, store, bq.Client(project=args.project), args.project, args.dataset)
        )
    )


if __name__ == "__main__":
    main()
