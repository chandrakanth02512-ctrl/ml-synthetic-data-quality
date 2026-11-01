"""Future approved create-only GCS staging from a reviewed GitHub checkout.

No bucket/API/IAM changes, builds, batch submission or execution. Never runs on import.
"""

import argparse
import json
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from package_lakehouse import package

from synthetic_quality_lakehouse.source import parse_source
from synthetic_quality_lakehouse.storage import GCSObjects

BUCKET = "gs://synthetic-quality-984592102391-data-as1"


def stage(source, revision, run_id, client):
    import hashlib

    from synthetic_quality_lakehouse.config import RunConfig

    payload = Path(source).read_bytes()
    if len(payload) > 10 * 1024 * 1024 or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Source exceeds pilot bound or revision is not a full commit")
    digest = hashlib.sha256(payload).hexdigest()
    parse_source(payload, digest)
    uri = BUCKET + "/lakehouse/inputs/" + run_id + ".ndjson"
    # Validate run/path/hash before any object write.
    RunConfig(uri, 1, digest, BUCKET + "/lakehouse/phase2", run_id)
    store = GCSObjects(client)
    input_record = store.create(uri, payload)
    code_root = BUCKET + "/lakehouse/code/" + revision
    with TemporaryDirectory(prefix="lakehouse-code-") as temporary:
        zip_path = Path(temporary) / "lakehouse.zip"
        package(zip_path)
        zip_record = store.create(code_root + "/lakehouse.zip", zip_path.read_bytes())
    entry = Path(__file__).resolve().parents[1] / "jobs/dataproc_quality.py"
    driver_record = store.create(code_root + "/dataproc_quality.py", entry.read_bytes())
    return {"source": input_record, "package": zip_record, "driver": driver_record}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--run-id", default="pilot-001")
    parser.add_argument("--approve-staging", action="store_true")
    args = parser.parse_args()
    if not args.approve_staging:
        parser.error("Requires separate explicit approval for GCS object creation")
    root = Path(__file__).resolve().parents[1]
    actual = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "src",
            "jobs",
            "tools/package_lakehouse.py",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if actual != args.revision or dirty:
        raise ValueError("Reviewed commit/clean source checkout required")
    from google.cloud import storage

    print(
        json.dumps(
            stage(
                args.input,
                args.revision,
                args.run_id,
                storage.Client(project="gcp-intelligent-data-quality"),
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
