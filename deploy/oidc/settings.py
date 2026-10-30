"""Reviewed non-secret coordinates. No cloud calls or credentials on import."""

import json
import re
from pathlib import Path

PROJECT = "gcp-intelligent-data-quality"
PROJECT_NUMBER = "984592102391"
REGION = "asia-south1"
REPO = "chandrakanth02512-ctrl/ml-synthetic-data-quality"
REPO_ID = "1436792250"
OWNER_ID = "341606319"
APP_REV = "34cc3d46b662a9f0560c3ff846d68b327d52e4b8"
JOB = "synthetic-quality-phase1-smoke"
REPOSITORY = "synthetic-quality-phase1"
IMAGE = "synthetic-quality"
RUNTIME_SA = f"synthetic-quality-runtime@{PROJECT}.iam.gserviceaccount.com"
DEPLOY_SA = f"synthetic-quality-deployer@{PROJECT}.iam.gserviceaccount.com"
EXECUTE_SA = f"synthetic-quality-executor@{PROJECT}.iam.gserviceaccount.com"
KNOWN_CANCELLED = "6f75c5dd-cc3a-4d47-9c9d-43f51fa3c4ba"
IMAGE_BASE = f"{REGION}-docker.pkg.dev/{PROJECT}/{REPOSITORY}/{IMAGE}"


def load_config(require_enabled=False, path=None):
    config = json.loads(Path(path or Path(__file__).with_name("config.json")).read_text())
    if set(config) != {
        "enabled",
        "image_digest",
        "replacement_build_id",
        "data_bucket",
        "run_id",
        "environment_bypass_disabled_attested",
    }:
        raise RuntimeError("Unexpected configuration fields; review required")
    if type(config["enabled"]) is not bool:
        raise RuntimeError("enabled must be a boolean")
    if config["data_bucket"] != f"synthetic-quality-{PROJECT_NUMBER}-data-as1":
        raise RuntimeError(
            "Only the reviewed planned data bucket is allowed; protected bucket excluded"
        )
    if config["run_id"] != "smoke-001":
        raise RuntimeError("Changing the run namespace requires separate review")
    attestations = config["environment_bypass_disabled_attested"]
    if (
        not isinstance(attestations, dict)
        or set(attestations) != {"gcp-deploy", "gcp-execute"}
        or any(type(v) is not bool for v in attestations.values())
    ):
        raise RuntimeError(
            "Explicit boolean approval-bypass attestations are required for both environments"
        )
    if require_enabled:
        if not config["enabled"]:
            raise RuntimeError(
                "Deployment is disabled pending verified image and owner authorization"
            )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", config["image_digest"] or ""):
            raise RuntimeError("Verified immutable image_digest is missing or invalid")
        if not re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", config["replacement_build_id"] or ""
        ):
            raise RuntimeError("Verified replacement_build_id is missing or invalid")
        if config["replacement_build_id"] == KNOWN_CANCELLED:
            raise RuntimeError("The cancelled build cannot authorize deployment")
        if not all(attestations.values()):
            raise RuntimeError(
                "Owner must review and attest that approval bypass is disabled "
                "for both environments"
            )
    return config


def job_name():
    return f"projects/{PROJECT}/locations/{REGION}/jobs/{JOB}"


def job_url():
    return "https://run.googleapis.com/v2/" + job_name()


def image_uri(config):
    return IMAGE_BASE + "@" + config["image_digest"]


def job_environment(config):
    return {
        "GCP_PROJECT_ID": PROJECT,
        "GCS_BUCKET": config["data_bucket"],
        "GCS_PREFIX": "synthetic-quality",
        "RUN_ID": config["run_id"],
        "TRAINING_ROWS": "256",
        "EVALUATION_ROWS": "100",
        "TRAINING_SEED": "42",
        "EVALUATION_SEED": "123",
    }
