"""Local-only, explicitly approved publication of pinned completed artifacts.

No training, Spark/Vertex clients, deployment or GCS writes. An immutable BigQuery
anchor binds the source/configuration to one attempt before any table mutations.
"""

import argparse
import hashlib
import json
import logging
import re
import subprocess
import time
from dataclasses import asdict
from importlib.resources import files
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import numpy as np
from google.api_core.exceptions import NotFound, ServiceUnavailable, TooManyRequests
from google.cloud import bigquery as bq
from requests.exceptions import ConnectionError, Timeout

from synthetic_quality.evaluation import evaluate_predictions
from synthetic_quality_lakehouse import bigquery as loader
from synthetic_quality_lakehouse.config import canonical, gcs_parts
from synthetic_quality_lakehouse.schemas import TABLES
from synthetic_quality_lakehouse.storage import GCSObjects
from synthetic_quality_vertex import inputs, predictions
from synthetic_quality_vertex.config import ObjectRef, code_digest, decode_rows, read_ref
from synthetic_quality_vertex.train import validate_completion
from synthetic_quality_workflows.plan import PROJECT, REGION, fingerprint, processing, validate
from synthetic_quality_workflows.runner import BoundedBQ, quality


def implementation_digest():
    digest = hashlib.sha256(code_digest().encode())
    for item in sorted(files("synthetic_quality_workflows").iterdir(), key=lambda p: p.name):
        if item.name.endswith(".py"):
            digest.update(item.name.encode() + b"\0" + item.read_bytes())
    return digest.hexdigest()


class RecoveryObjects(GCSObjects):
    """Two read attempts only; mutations and BigQuery submissions are never retried here."""

    def read(self, uri, generation=None, max_bytes=64 * 1024 * 1024):
        bucket, name = gcs_parts(uri)
        for attempt in (1, 2):
            operation = "metadata"
            try:
                blob = self.client.bucket(bucket).get_blob(
                    name, generation=generation, retry=None, timeout=(10, 30)
                )
                if blob is None:
                    raise FileNotFoundError("Required object absent: " + uri)
                if blob.size is None or blob.size > max_bytes:
                    raise ValueError("Object exceeds reviewed driver-memory bound")
                operation = "download"
                data = blob.download_as_bytes(
                    if_generation_match=int(blob.generation), retry=None, timeout=(10, 30)
                )
                if len(data) > max_bytes:
                    raise ValueError("Object exceeds reviewed driver-memory bound")
                return data, int(blob.generation)
            except (ConnectionError, Timeout, ServiceUnavailable, TooManyRequests) as error:
                detail = (
                    f"GCS {operation} failed: uri={uri}, requested_generation={generation}, "
                    f"attempt={attempt}/2, timeout=(10,30), error={type(error).__name__}"
                )
                logging.warning(detail)
                if attempt == 2:
                    raise RuntimeError(detail) from error
                time.sleep(1)


class ReadOnlyObjects:
    def __init__(self, store):
        self.store = store

    def read(self, *args, **kwargs):
        return self.store.read(*args, **kwargs)

    def verify(self, ref):
        return self.store.verify(ref)


def original_code_digest(package):
    digest = hashlib.sha256()
    with ZipFile(BytesIO(package)) as archive:
        for package in (
            "synthetic_quality",
            "synthetic_quality_lakehouse",
            "synthetic_quality_vertex",
        ):
            for name in sorted(archive.namelist()):
                if name.startswith(package + "/") and name.count("/") == 1 and name.endswith(".py"):
                    digest.update(name.encode() + b"\0" + archive.read(name))
            if package == "synthetic_quality_lakehouse":
                for name in ("publish.sql", "reconcile.sql"):
                    digest.update(archive.read(package + "/sql/" + name))
    return digest.hexdigest()


def verify(manifest, store):
    required = {
        "format_version",
        "attempt_id",
        "project",
        "region",
        "plan",
        "training",
        "evaluation",
        "model",
        "superseded_job",
        "implementation_sha256",
        "maximum_bytes_billed",
    }
    if set(manifest) != required or manifest["format_version"] != 1:
        raise ValueError("Exact versioned recovery manifest required")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", manifest["attempt_id"]):
        raise ValueError("Invalid immutable attempt ID")
    if manifest["project"] != PROJECT or manifest["region"] != REGION:
        raise ValueError("Unexpected recovery project/region")
    if manifest["implementation_sha256"] != implementation_digest():
        raise ValueError("Recovery code differs from reviewed manifest")
    if manifest["maximum_bytes_billed"] != loader.PUBLICATION_MAX_BYTES_BILLED:
        raise ValueError("Publication budget differs")
    if not re.fullmatch(r"idq_[0-9a-f]{32}_publish", manifest["superseded_job"]):
        raise ValueError("Exact failed publication identity required")
    for name in ("plan", "training", "evaluation", "model"):
        ObjectRef(**manifest[name])
        store.verify(manifest[name])
    plan = json.loads(read_ref(store, ObjectRef(**manifest["plan"])))
    validate(plan)
    if not plan["enabled"] or fingerprint(plan) != manifest["plan"]["sha256"]:
        raise ValueError("Original plan identity differs")
    for name in ("training", "evaluation", "truth", "package", "bootstrap"):
        store.verify(plan[name])
    splits = {}
    for name in ("training", "evaluation"):
        ref = ObjectRef(**manifest[name])
        if ref.uri != processing(plan, name).root + "/completion.json":
            raise ValueError("Unexpected Spark manifest path")
        splits[name] = json.loads(read_ref(store, ref))
        quality(plan, name, splits[name], store)
        if (
            splits[name]["source_rows"] != plan[name + "_rows"]
            or splits[name]["artifacts"]["quarantine"]["row_count"] != 0
        ):
            raise ValueError("Recovery requires the reviewed clean 256/100 splits")
    model = json.loads(read_ref(store, ObjectRef(**manifest["model"])))
    config = validate_completion(model)
    if (
        config.run_id != plan["run_id"]
        or asdict(config.training) != manifest["training"]
        or asdict(config.evaluation) != manifest["evaluation"]
        or asdict(config.truth) != plan["truth"]
    ):
        raise ValueError("Model source references differ")
    if manifest["model"]["uri"] != config.root + "/completion.json":
        raise ValueError("Unexpected model manifest path")
    if original_code_digest(store.verify(plan["package"])) != model["request"]["code_sha256"]:
        raise ValueError("Original model code provenance differs")
    for ref in model["artifacts"].values():
        store.verify(ref)
    metadata = json.loads(store.verify(model["artifacts"]["metadata"]))
    if (
        not metadata["model_reload_verified"]
        or metadata["model_sha256"] != model["artifacts"]["model"]["sha256"]
    ):
        raise ValueError("Model reload/hash verification absent")
    _, evaluation, _, _ = inputs.splits(store, config)
    truth = inputs.truth_labels(store, config, evaluation)
    rows = decode_rows(store.verify(model["artifacts"]["predictions"]))
    if any(
        set(row) != {name for name, _ in predictions.PREDICTIONS}
        or not np.isfinite(row["anomaly_score"])
        or any(
            type(row[k]) is not int or row[k] not in (0, 1)
            for k in (
                "actual_anomaly",
                "rule_based_prediction",
                "isolation_forest_prediction",
                "combined_or_prediction",
            )
        )
        or row["combined_or_prediction"]
        != max(row["rule_based_prediction"], row["isolation_forest_prediction"])
        for row in rows
    ):
        raise ValueError("Prediction schema/values differ")
    by_id = {r["transaction_id"]: r for r in rows}
    if len(by_id) != len(rows) or set(by_id) != set(evaluation.transaction_id):
        raise ValueError("Prediction identities differ")
    ordered = [by_id[k] for k in evaluation.transaction_id]
    for row, expected, actual in zip(ordered, evaluation.to_dict("records"), truth, strict=True):
        if (
            row["actual_anomaly"] != int(actual)
            or row["source_row_id"] != expected["source_row_id"]
            or row["source_record_sha256"] != expected["source_record_sha256"]
            or row["model_sha256"] != model["artifacts"]["model"]["sha256"]
            or row["run_id"] != config.run_id
            or row["source_fingerprint"] != model["fingerprint"]
        ):
            raise ValueError("Prediction lineage/truth/model differs")
    metrics = {
        name: evaluate_predictions(truth, np.array([r[field] for r in ordered]))
        for name, field in {
            "rule_based": "rule_based_prediction",
            "isolation_forest": "isolation_forest_prediction",
            "combined_or": "combined_or_prediction",
        }.items()
    }
    if metrics != json.loads(store.verify(model["artifacts"]["metrics"])):
        raise ValueError("Saved metrics differ from immutable predictions/truth")
    if (
        model["training_rows"] != plan["training_rows"]
        or model["evaluation_rows"] != plan["evaluation_rows"]
    ):
        raise ValueError("Model row bounds differ")
    return plan, splits, model, metrics


class AttemptClient:
    def __init__(self, client, scope, manifest_sha, labels):
        self.client, self.scope, self.manifest_sha = client, scope, manifest_sha
        self.labels = labels
        self.phase = None

    def publication_id(self, phase):
        return "pubrec_" + self.scope + "_" + phase

    def mapped(self, job_id):
        return self.publication_id(self.phase) if job_id.endswith("_publish") else job_id

    def get_job(self, job_id, **kwargs):
        return self.client.get_job(self.mapped(job_id), **kwargs)

    def query(self, sql, *, job_config, job_id, **kwargs):
        job_config.labels = {
            **(job_config.labels or {}),
            **self.labels,
            "recovery_sha_head": self.manifest_sha[:32],
            "recovery_sha_tail": self.manifest_sha[32:],
        }
        return self.client.query(sql, job_config=job_config, job_id=self.mapped(job_id), **kwargs)

    def __getattr__(self, name):
        if name not in {"get_dataset", "get_table", "create_table", "load_table_from_uri"}:
            raise AttributeError(name)
        return getattr(self.client, name)


def recover(manifest, objects, client, seconds=600):
    store = ReadOnlyObjects(objects)
    plan, splits, model, metrics = verify(manifest, store)
    ds = PROJECT + "." + plan["dataset"]
    legacy_token = fingerprint(
        {
            "run": plan["run_id"] + "-training",
            "fingerprint": splits["training"]["fingerprint"],
            "dataset": ds,
            "location": REGION,
        }
    )[:32]
    if manifest["superseded_job"] != "idq_" + legacy_token + "_publish":
        raise ValueError("Failed publication job does not match the pinned training source")
    bounded = BoundedBQ(client, seconds=seconds)
    previous = bounded.get_job(manifest["superseded_job"], location=REGION, retry=None)
    if getattr(
        previous, "state", None
    ) != "DONE" or "Query exceeded limit for bytes billed" not in str(
        getattr(previous, "error_result", None)
    ):
        raise ValueError("Original failed job is not terminal with the reviewed billing error")
    parameters = (
        previous.to_api_repr().get("configuration", {}).get("query", {}).get("queryParameters", [])
    )
    if not any(
        p["name"] == "run_id" and p["parameterValue"].get("value") == plan["run_id"] + "-training"
        for p in parameters
    ):
        raise ValueError("Failed job belongs to another source run")
    if bounded.get_dataset(ds, retry=None).location.lower() != REGION:
        raise ValueError("Dataset region differs")
    for kind, name in TABLES.items():
        loader.validate_table(bounded.get_table(ds + "." + name, retry=None), kind)
    for name, fields in (
        ("anomaly_predictions", predictions.PREDICTIONS),
        ("model_runs", predictions.RUNS),
    ):
        predictions.validate_table(bounded.get_table(ds + "." + name, retry=None), fields)
    # Scope excludes attempt ID: competing attempts for the same source/configuration
    # collide at this immutable anchor, rather than publishing concurrently.
    scope = fingerprint({k: v for k, v in manifest.items() if k != "attempt_id"})[:32]
    sha = fingerprint(manifest)
    labels = {
        "recovery_attempt": manifest["attempt_id"],
        "source_run": plan["run_id"],
    }
    api = AttemptClient(bounded, scope, sha, labels)
    anchor_id = api.publication_id("anchor")
    config = bq.QueryJobConfig(
        use_legacy_sql=False,
        maximum_bytes_billed=loader.PUBLICATION_MAX_BYTES_BILLED,
        labels=labels,
        query_parameters=[bq.ScalarQueryParameter("recovery_sha", "STRING", sha)],
    )
    sql = "SELECT @recovery_sha AS recovery_sha"
    expected = config.to_api_repr()
    expected["query"]["query"] = sql
    loader.job_once(
        bounded,
        anchor_id,
        REGION,
        expected,
        lambda: bounded.query(
            sql, job_config=config, job_id=anchor_id, location=REGION, retry=None, job_retry=None
        ),
    )
    # Fail before any table writes if this attempt already has a failed publication.
    for phase in ("training", "evaluation", "predictions"):
        try:
            job = bounded.get_job(api.publication_id(phase), location=REGION, retry=None)
        except NotFound:
            continue
        if getattr(job, "error_result", None) or getattr(job, "error", None):
            raise ValueError("Existing recovery job failed; no automatic replacement")
    receipts = {}
    for phase in ("training", "evaluation", "predictions"):
        api.phase = phase
        receipts[phase] = (
            predictions.load(model, store, api, PROJECT, plan["dataset"], REGION)
            if phase == "predictions"
            else loader.load(splits[phase], store, api, PROJECT, plan["dataset"], REGION)
        )
    # Fresh hash/generation/lineage verification after publication, with no GCS writes.
    verify(manifest, store)
    counts = {}
    for name in (*TABLES.values(), "anomaly_predictions", "model_runs"):
        remaining = bounded.deadline - bounded.clock()
        if remaining <= 0:
            raise TimeoutError("Recovery deadline expired; inspect existing jobs")
        counts[name] = len(
            list(
                client.list_rows(
                    ds + "." + name,
                    max_results=1000,
                    page_size=1000,
                    retry=None,
                    timeout=min(30, remaining),
                )
            )
        )
    expected_counts = {
        "clean_transactions": plan["training_rows"] + plan["evaluation_rows"],
        "quarantined_records": 0,
        "quality_results": sum(m["artifacts"]["quality"]["row_count"] for m in splits.values()),
        "pipeline_runs": 2,
        "anomaly_predictions": plan["evaluation_rows"],
        "model_runs": 1,
    }
    if counts != expected_counts:
        raise ValueError("Final table counts differ; preserve partial receipts and inspect")
    return {
        "status": "COMPLETE",
        "attempt_id": manifest["attempt_id"],
        "manifest_sha256": sha,
        "source_run_id": plan["run_id"],
        "anchor_job_id": anchor_id,
        "publications": receipts,
        "counts": counts,
        "metrics": metrics,
        "model_sha256": model["artifacts"]["model"]["sha256"],
        "original_workflow_status": "FAILED; preserved",
        "gcs_writes": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--approve-publication", action="store_true")
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--account", required=True)
    args = parser.parse_args()
    raw = args.manifest.read_bytes()
    manifest = json.loads(raw)
    if (
        not args.approve_publication
        or hashlib.sha256(raw).hexdigest() != args.sha256
        or raw != canonical(manifest)
        or args.confirm != "PUBLISH " + manifest["attempt_id"]
        or args.output.exists()
    ):
        parser.error(
            "Explicit approval, canonical manifest/hash/confirmation and new local output required"
        )
    # CLI credentials are read into memory only; no login, config change or key files.
    from google.cloud import storage
    from google.oauth2.credentials import Credentials

    token = subprocess.run(
        ["gcloud", "auth", "print-access-token", "--account=" + args.account],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    credentials = Credentials(token)
    objects = RecoveryObjects(storage.Client(project=PROJECT, credentials=credentials))
    client = bq.Client(project=PROJECT, credentials=credentials)
    with args.output.open("x") as stream:
        try:
            result = recover(manifest, objects, client)
        except Exception as error:
            stream.write(
                json.dumps(
                    {
                        "status": "STOPPED",
                        "attempt_id": manifest["attempt_id"],
                        "error": str(error),
                        "automatic_retries": 0,
                    },
                    indent=2,
                )
                + "\n"
            )
            raise
        stream.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
