"""Vertex CustomJob plan, one exclusive submission and read-only reconciliation."""

import argparse
import json
import re
from dataclasses import asdict, dataclass
from importlib.resources import files
from urllib.parse import urlencode

from google.api_core.exceptions import PreconditionFailed

from synthetic_quality_lakehouse.config import canonical, gcs_parts
from synthetic_quality_lakehouse.storage import GCSObjects
from synthetic_quality_vertex.config import (
    IMAGE,
    PROJECT,
    REGION,
    ObjectRef,
    TrainingConfig,
    fingerprint,
    read_ref,
)

ACCOUNT = "synthetic-quality-vertex@gcp-intelligent-data-quality.iam.gserviceaccount.com"
PARENT = f"projects/{PROJECT}/locations/{REGION}/customJobs"


@dataclass(frozen=True)
class JobPlan:
    training: TrainingConfig
    config: ObjectRef
    package: ObjectRef

    def __post_init__(self):
        if self.config.sha256 != fingerprint(self.training.to_dict()):
            raise ValueError(
                "Config reference must bind the exact canonical training configuration"
            )

    @classmethod
    def from_dict(cls, value):
        return cls(
            TrainingConfig.from_dict(value["training"]),
            ObjectRef(**value["config"]),
            ObjectRef(**value["package"]),
        )

    def spec(self):
        bootstrap = files("synthetic_quality_vertex").joinpath("bootstrap.py").read_text()
        args = [
            str(v)
            for ref in (self.package, self.config)
            for v in (ref.uri, ref.generation, ref.sha256)
        ]
        return {
            "displayName": "quality-" + self.training.run_id,
            "labels": {
                "application": "synthetic-quality-vertex",
                "run_id": self.training.run_id,
                "request_sha": fingerprint(asdict(self))[:40],
            },
            "jobSpec": {
                "serviceAccount": ACCOUNT,
                "baseOutputDirectory": {"outputUriPrefix": self.training.root + "/vertex-output"},
                "scheduling": {
                    "timeout": "600s",
                    "restartJobOnWorkerRestart": False,
                    "disableRetries": True,
                },
                "workerPoolSpecs": [
                    {
                        "machineSpec": {"machineType": "n1-standard-4"},
                        "replicaCount": "1",
                        "containerSpec": {
                            "imageUri": IMAGE,
                            "command": ["python", "-c", bootstrap],
                            "args": args,
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


class VertexObjects(GCSObjects):
    def reserve(self, uri, data):
        bucket, name = gcs_parts(uri)
        try:
            self.client.bucket(bucket).blob(name).upload_from_string(
                data, content_type="application/json", if_generation_match=0, retry=None
            )
        except PreconditionFailed:
            raise ValueError(
                "Submission already reserved; reconcile, never automatically retry"
            ) from None
        if self.read(uri)[0] != data:
            raise ValueError("Submission reservation verification failed")


class VertexAPI:
    def __init__(self):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        # A 401 must not trigger an automatic second POST.
        self.session = AuthorizedSession(credentials, max_refresh_attempts=0)

    def request(self, method, path, body=None):
        if (
            method not in {"GET", "POST"}
            or not path.startswith(PARENT)
            or (method == "POST" and path != PARENT)
        ):
            raise ValueError("Unsupported Vertex operation")
        import requests

        try:
            response = self.session.request(
                method,
                f"https://{REGION}-aiplatform.googleapis.com/v1/" + path,
                json=body,
                timeout=60,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise RuntimeError(
                "Vertex response ambiguous; preserve reservation, no automatic retry"
            ) from None
        if response.status_code != 200:
            raise RuntimeError(f"Vertex HTTP {response.status_code}; stop without retry")
        return response.json()


def validate_job(job, plan):
    expected = plan.spec()
    if not re.fullmatch(re.escape(PARENT) + r"/[0-9]+", job.get("name", "")):
        raise ValueError("Unexpected CustomJob identity")

    def includes(actual, reviewed):
        if isinstance(reviewed, dict):
            return isinstance(actual, dict) and all(
                (v is False and k not in actual)  # Proto3 omits false scalar defaults.
                or (k in actual and includes(actual[k], v))
                for k, v in reviewed.items()
            )
        if isinstance(reviewed, list):
            return (
                isinstance(actual, list)
                and len(actual) == len(reviewed)
                and all(includes(a, b) for a, b in zip(actual, reviewed, strict=True))
            )
        return actual == reviewed

    if not includes(job, expected):
        raise ValueError("CustomJob differs from the reviewed request; labels alone are not proof")
    worker = job["jobSpec"]["workerPoolSpecs"][0]
    if (
        worker["machineSpec"].get("acceleratorCount", 0) != 0
        or worker["machineSpec"].get("acceleratorType", "ACCELERATOR_TYPE_UNSPECIFIED")
        != "ACCELERATOR_TYPE_UNSPECIFIED"
        or any(
            job["jobSpec"].get(k)
            for k in (
                "persistentResourceId",
                "enableWebAccess",
                "enableDashboardAccess",
                "network",
                "tensorboard",
            )
        )
    ):
        raise ValueError("Unexpected accelerator, persistent resource or web access")
    return job


def submit(plan, store, api):
    # Verify exact staged code/config before reserving one non-idempotent create call.
    read_ref(store, plan.package, 4 * 1024 * 1024)
    if read_ref(store, plan.config) != canonical(plan.training.to_dict()):
        raise ValueError("Staged configuration differs")
    attempt = plan.training.root + "/submission.attempt.json"
    store.reserve(attempt, canonical(plan.spec()))
    job = validate_job(api.request("POST", PARENT, plan.spec()), plan)
    store.create(plan.training.root + "/submission.receipt.json", canonical({"name": job["name"]}))
    return {"status": "SUBMITTED", "name": job["name"], "training_verified": False}


def reconcile(plan, store, api):
    attempt = store.read(plan.training.root + "/submission.attempt.json")[0]
    if attempt != canonical(plan.spec()):
        raise ValueError("Reservation differs from plan")
    receipt = store.optional(plan.training.root + "/submission.receipt.json")
    if receipt:
        name = json.loads(receipt[0])["name"]
        if not re.fullmatch(re.escape(PARENT) + r"/[0-9]+", name):
            raise ValueError("Unexpected receipt name")
        jobs = [api.request("GET", name)]
    else:
        jobs, seen, token = [], set(), None
        while True:
            query = {"filter": 'labels.request_sha="' + plan.spec()["labels"]["request_sha"] + '"'}
            if token:
                query["pageToken"] = token
            page = api.request("GET", PARENT + "?" + urlencode(query))
            jobs.extend(page.get("customJobs", []))
            token = page.get("nextPageToken")
            if not token:
                break
            if token in seen:
                raise ValueError("Incomplete CustomJob inventory")
            seen.add(token)
    if len(jobs) != 1:
        raise ValueError("Cannot uniquely reconcile submission; preserve marker and stop")
    job = validate_job(jobs[0], plan)
    return {
        "name": job["name"],
        "state": job.get("state", "UNVERIFIED"),
        "training_verified": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "submit", "reconcile"))
    parser.add_argument("--plan", required=True)
    parser.add_argument("--approve-submission", action="store_true")
    args = parser.parse_args()
    with open(args.plan) as stream:
        plan = JobPlan.from_dict(json.load(stream))
    if args.action == "plan":
        print(json.dumps(plan.spec(), indent=2))
        return
    if args.action == "submit" and not args.approve_submission:
        parser.error("Separate explicit CustomJob execution approval required")
    from google.cloud import storage

    store = VertexObjects(storage.Client(project=PROJECT))
    api = VertexAPI()
    print(
        json.dumps(
            submit(plan, store, api) if args.action == "submit" else reconcile(plan, store, api)
        )
    )


if __name__ == "__main__":
    main()
