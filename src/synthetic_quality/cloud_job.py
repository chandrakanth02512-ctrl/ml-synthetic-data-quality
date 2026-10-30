"""Finite Cloud Run Job: reuse the verified pipeline, publish artifacts, then commit."""

import hashlib
import json
import platform
import sys
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import DefaultCredentialsError, RefreshError
from google.cloud import storage

from synthetic_quality.cloud_config import CloudConfig, parse_config
from synthetic_quality.gcs import GCSStore, RunConflictError
from synthetic_quality.pipeline import run_pipeline

# Explicit allowlist: never upload arbitrary files from a directory or external input.
ARTIFACTS = {
    "data/training.csv": "raw-data",
    "data/evaluation.csv": "raw-data",
    "data/evaluation_truth.csv": "raw-data",
    "reports/data_quality.json": "quality-reports",
    "reports/data_quality.csv": "quality-reports",
    "reports/evaluation.json": "evaluation-reports",
    "reports/evaluation.csv": "evaluation-reports",
    "reports/predictions.csv": "evaluation-reports",
    "models/isolation_forest.joblib": "models",
    "models/model_metadata.json": "models",
    "models/rule_baseline.json": "models",
    "reports/run_manifest.json": "run-manifests",
}


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def execution_identity(config: CloudConfig) -> dict:
    source = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        source.update(path.name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return {
        "schema_version": 1,
        "config": asdict(config),
        "pipeline_config": asdict(config.pipeline_config()),
        "source_sha256": source.hexdigest(),
        "python_version": platform.python_version(),
        "package_versions": {
            name: version(name)
            for name in (
                "ml-synthetic-data-quality",
                "numpy",
                "pandas",
                "scikit-learn",
                "scipy",
                "joblib",
                "google-cloud-storage",
            )
        },
    }


def run_cloud_job(config: CloudConfig, *, client: storage.Client | None = None) -> dict:
    """Resume matching partial runs; verify completed runs without training again.

    A request reserves the run ID for exact configuration, source, and library
    versions. All writes are create-only. Consumers must require completion.json;
    request.json and the pipeline's run_manifest.json are not completion markers.
    """
    store = GCSStore(
        client if client is not None else storage.Client(project=config.project_id), config.bucket
    )
    request = execution_identity(config)
    store.create(
        config.object_name("run-manifests", "request.json"), json_bytes(request), "application/json"
    )
    completion_name = config.object_name("run-manifests", "completion.json")
    names = {
        local: config.object_name(category, Path(local).name)
        for local, category in ARTIFACTS.items()
    }
    completed = store.read(completion_name)
    if completed is not None:
        try:
            manifest = json.loads(completed.data)
        except (ValueError, UnicodeDecodeError) as error:
            raise RunConflictError("Invalid completion manifest; use a new run_id") from error
        if (
            not isinstance(manifest, dict)
            or manifest.get("status") != "complete"
            or manifest.get("request") != request
            or not isinstance(manifest.get("artifacts"), dict)
            or set(manifest["artifacts"]) != set(names)
        ):
            raise RunConflictError("Completion manifest does not match this run; use a new run_id")
        for local, name in names.items():
            existing = store.read(name)
            if existing is None or existing.record() != manifest["artifacts"][local]:
                raise RunConflictError(f"Completed artifact missing or changed: {name}")
        print(f"Run {config.run_id}: already complete; verified {len(names)} artifacts.")
        return manifest

    with TemporaryDirectory(prefix="synthetic-quality-") as directory:
        run_pipeline(directory, config.pipeline_config())
        records = {}
        for local, name in names.items():
            path = Path(directory) / local
            content_type = {".csv": "text/csv", ".json": "application/json"}.get(
                path.suffix, "application/octet-stream"
            )
            records[local] = store.create(name, path.read_bytes(), content_type).record()
        manifest = {"status": "complete", "request": request, "artifacts": records}
        # This is the commit point; never reached after any pipeline/upload failure.
        store.create(completion_name, json_bytes(manifest), "application/json")
    print(f"Run {config.run_id}: complete; gs://{config.bucket}/{completion_name}")
    return manifest


def main(argv: list[str] | None = None) -> None:
    config = parse_config(argv)
    try:
        run_cloud_job(config)
    except (DefaultCredentialsError, RefreshError):
        print(
            "Cloud authentication failed. Attach a dedicated runtime service account with ADC; "
            "do not download service-account keys.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except GoogleAPIError as error:
        print(
            f"GCS operation failed ({type(error).__name__}). Check bucket, IAM and network; "
            "retry with the same run_id and image after fixing the cause.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except (RunConflictError, ValueError, OSError, RuntimeError) as error:
        print(f"Cloud job failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
