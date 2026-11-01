"""Compile a fixed, reviewed pilot; execution arguments cannot replace workloads."""

import base64
import hashlib
import re
from dataclasses import asdict, dataclass
from importlib.resources import files
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from synthetic_quality_lakehouse.config import RunConfig, canonical, dataset_name, gcs_parts

# Keep the Spark bootstrap independent of sklearn and the Vertex SDK/model imports.
PROJECT = "gcp-intelligent-data-quality"
REGION = "asia-south1"


@dataclass(frozen=True)
class ObjectRef:
    uri: str
    generation: int
    sha256: str

    def __post_init__(self):
        gcs_parts(self.uri)
        if (
            type(self.generation) is not int
            or self.generation < 1
            or not re.fullmatch(r"[0-9a-f]{64}", self.sha256)
        ):
            raise ValueError("Exact object generation and SHA-256 required")


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


BUCKET = "synthetic-quality-984592102391-data-as1"
ROOT = f"gs://{BUCKET}/lakehouse/orchestration"
SPARK_ACCOUNT = f"synthetic-quality-spark@{PROJECT}.iam.gserviceaccount.com"
VERTEX_ACCOUNT = f"synthetic-quality-vertex@{PROJECT}.iam.gserviceaccount.com"


def object_url(uri):
    bucket, name = gcs_parts(uri)
    return f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/" + quote(name, safe="")


def validate(plan):
    required = {
        "format_version",
        "enabled",
        "run_id",
        "training",
        "evaluation",
        "truth",
        "package",
        "bootstrap",
        "runtime_image",
        "subnetwork",
        "dataset",
        "training_rows",
        "evaluation_rows",
        "max_quarantined_rows",
        "poll_seconds",
        "poll_limit",
        "revision",
    }
    if set(plan) != required or plan["format_version"] != 1 or type(plan["enabled"]) is not bool:
        raise ValueError("Exact versioned pilot configuration required")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", plan["run_id"]):
        raise ValueError("Invalid pilot run ID")
    if not re.fullmatch(r"[0-9a-f]{40}", plan["revision"]):
        raise ValueError("Reviewed feature revision required")
    refs = {}
    for name in ("training", "evaluation", "truth", "package", "bootstrap"):
        refs[name] = ObjectRef(**plan[name])
        if gcs_parts(refs[name].uri)[0] != BUCKET:
            raise ValueError("Use the existing dedicated data bucket")
    if refs["training"].uri == refs["evaluation"].uri or (
        refs["training"].sha256 == refs["evaluation"].sha256
    ):
        raise ValueError("Training/evaluation sources must differ")
    if not re.fullmatch(
        rf"{REGION}-docker\.pkg\.dev/{PROJECT}/synthetic-quality-phase1/"
        r"synthetic-quality-workflows@sha256:[0-9a-f]{64}",
        plan["runtime_image"],
    ):
        raise ValueError("A separately built, verified workflow runtime digest is required")
    if not re.fullmatch(
        rf"projects/{PROJECT}/regions/{REGION}/subnetworks/[a-z][a-z0-9-]{{0,62}}",
        plan["subnetwork"],
    ):
        raise ValueError("Reviewed Mumbai subnetwork required")
    dataset_name(PROJECT, plan["dataset"])
    for name, low, high in (
        ("training_rows", 2, 256),
        ("evaluation_rows", 1, 100),
        ("max_quarantined_rows", 0, 10),
        ("poll_seconds", 5, 60),
        ("poll_limit", 1, 120),
    ):
        if type(plan[name]) is not int or not low <= plan[name] <= high:
            raise ValueError("Pilot size/polling outside reviewed bounds: " + name)
    return refs


def processing(plan, split):
    ref = ObjectRef(**plan[split])
    return RunConfig(
        ref.uri,
        ref.generation,
        ref.sha256,
        ROOT + "/data",
        plan["run_id"] + "-" + split,
        max_rows=266,
    )


def root(plan):
    return ROOT + "/runs/" + plan["run_id"]


def compile_plan(plan, reference):
    refs = validate(plan)
    ref = ObjectRef(**reference)
    if ref.sha256 != fingerprint(plan) or not ref.uri.startswith(ROOT + "/plans/"):
        raise ValueError("Plan reference must bind canonical reviewed bytes")
    token = fingerprint(plan)
    args = [str(v) for r in (refs["package"], ref) for v in (r.uri, r.generation, r.sha256)]
    stages = []
    for split in ("training", "evaluation"):
        batch_id = "idq-" + token[:32] + "-" + split
        spec = {
            "pysparkBatch": {
                "mainPythonFileUri": refs["bootstrap"].uri,
                "args": [*args, split],
            },
            "runtimeConfig": {
                "version": "3.0",
                "properties": {
                    "dataproc.tier": "standard",
                    "spark.driver.cores": "4",
                    "spark.driver.memory": "8g",
                    "spark.executor.cores": "4",
                    "spark.executor.memory": "8g",
                    "spark.executor.instances": "2",
                    "spark.dynamicAllocation.enabled": "false",
                    "spark.sql.shuffle.partitions": "2",
                    "spark.sql.session.timeZone": "UTC",
                },
            },
            "environmentConfig": {
                "executionConfig": {
                    "serviceAccount": SPARK_ACCOUNT,
                    "subnetworkUri": plan["subnetwork"],
                    "ttl": "600s",
                    "authenticationConfig": {
                        "userWorkloadAuthenticationType": "SERVICE_ACCOUNT",
                    },
                }
            },
            "labels": {"application": "synthetic-quality-workflows", "request_sha": token[:40]},
        }
        stages.append(
            {
                "id": split,
                "kind": "dataproc",
                "spec": spec,
                "collection": f"https://dataproc.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/batches",
                "name": f"projects/{PROJECT}/locations/{REGION}/batches/{batch_id}",
                "query": {
                    "batchId": batch_id,
                    "requestId": str(uuid5(NAMESPACE_URL, token + split)),
                },
                "result_url": object_url(processing(plan, split).root + "/completion.json"),
            }
        )
    bootstrap = files("synthetic_quality_workflows").joinpath("bootstrap.py").read_text()
    spec = {
        "displayName": "quality-" + plan["run_id"],
        "labels": {"application": "synthetic-quality-workflows", "request_sha": token[:40]},
        "jobSpec": {
            "serviceAccount": VERTEX_ACCOUNT,
            "baseOutputDirectory": {"outputUriPrefix": root(plan) + "/vertex-output"},
            "scheduling": {
                "timeout": "1200s",
                "disableRetries": True,
                "restartJobOnWorkerRestart": False,
            },
            "workerPoolSpecs": [
                {
                    "machineSpec": {"machineType": "n1-standard-4"},
                    "replicaCount": "1",
                    "containerSpec": {
                        "imageUri": plan["runtime_image"],
                        "command": ["python", "-c", bootstrap],
                        "args": [*args, "vertex"],
                        "env": [
                            {"name": n, "value": "1"}
                            for n in (
                                "OMP_NUM_THREADS",
                                "OPENBLAS_NUM_THREADS",
                                "MKL_NUM_THREADS",
                            )
                        ],
                    },
                }
            ],
        },
    }
    stages.append(
        {
            "id": "vertex",
            "kind": "vertex",
            "spec": spec,
            "collection": f"https://{REGION}-aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/customJobs",
            "query": {},
            "name": "",
            "result_url": object_url(root(plan) + "/completion.json"),
        }
    )
    return {
        "enabled": plan["enabled"],
        "run_id": plan["run_id"],
        "plan_hash": token,
        "bucket": BUCKET,
        "prefix": gcs_parts(root(plan))[1],
        "stages": stages,
        "poll_seconds": plan["poll_seconds"],
        "poll_limit": plan["poll_limit"],
        "objects": [
            {
                "url": object_url(r.uri),
                "generation": str(r.generation),
                "max_size": 4 * 1024 * 1024 if n == "package" else 10 * 1024 * 1024,
            }
            for n, r in {**refs, "plan": ref}.items()
        ],
        "bootstrap_url": object_url(refs["bootstrap"].uri),
        "bootstrap_generation": str(refs["bootstrap"].generation),
        "bootstrap_hash": base64.b64encode(bytes.fromhex(refs["bootstrap"].sha256)).decode(),
        "reference": asdict(ref),
        "training_rows": plan["training_rows"],
        "evaluation_rows": plan["evaluation_rows"],
    }
