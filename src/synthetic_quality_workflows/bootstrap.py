"""Standalone Spark/container bootstrap: verify package and plan before importing."""

import hashlib
import json
import runpy
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from google.cloud import storage


def download(client, uri, generation, digest, maximum):
    parsed = urlsplit(uri)
    if (
        parsed.scheme != "gs"
        or parsed.netloc != "synthetic-quality-984592102391-data-as1"
        or parsed.query
        or parsed.fragment
        or any(p in {"", ".", ".."} for p in parsed.path[1:].split("/"))
    ):
        raise ValueError("Invalid workflow bootstrap object")
    blob = client.bucket(parsed.netloc).get_blob(
        parsed.path[1:],
        generation=int(generation),
        retry=None,
    )
    if blob is None or blob.size is None or blob.size > maximum:
        raise ValueError("Missing or oversized workflow bootstrap object")
    data = blob.download_as_bytes(if_generation_match=int(generation), retry=None)
    if len(data) > maximum or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Workflow bootstrap hash differs")
    return data


def main():
    if len(sys.argv) != 8 or sys.argv[-1] not in {"training", "evaluation", "vertex"}:
        raise ValueError("Expected pinned package, plan and fixed phase")
    client = storage.Client()
    package = download(client, *sys.argv[1:4], maximum=4 * 1024 * 1024)
    plan = download(client, *sys.argv[4:7], maximum=64 * 1024)
    value = json.loads(plan)
    if value["package"] != {
        "uri": sys.argv[1],
        "generation": int(sys.argv[2]),
        "sha256": sys.argv[3],
    }:
        raise ValueError("Package does not match the pinned plan")
    with TemporaryDirectory(prefix="workflow-bootstrap-") as directory:
        path = Path(directory) / "pipeline.zip"
        path.write_bytes(package)
        sys.path.insert(0, str(path))
        config = Path(directory) / "plan.json"
        config.write_bytes(plan)
        sys.argv = [
            "synthetic_quality_workflows.runner",
            "--plan",
            str(config),
            "--phase",
            sys.argv[-1],
        ]
        runpy.run_module("synthetic_quality_workflows.runner", run_name="__main__")


if __name__ == "__main__":
    main()
