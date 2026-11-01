"""Standalone container command: verify code/config bytes before importing training."""

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
        or not parsed.netloc
        or not parsed.path.strip("/")
        or parsed.query
        or parsed.fragment
        or parsed.netloc == "gcp-idq-data-984592102391"
        or any(p in {"", ".", ".."} for p in parsed.path[1:].split("/"))
    ):
        raise ValueError("Invalid bootstrap object")
    blob = client.bucket(parsed.netloc).get_blob(
        parsed.path[1:], generation=int(generation), retry=None
    )
    if blob is None or blob.size is None or blob.size > maximum:
        raise ValueError("Missing or oversized bootstrap object")
    data = blob.download_as_bytes(if_generation_match=int(generation), retry=None)
    if len(data) > maximum or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Bootstrap input differs from reviewed bytes")
    return data


def main():
    if len(sys.argv) != 7:
        raise ValueError("Expected pinned code and configuration references")
    client = storage.Client()
    code = download(client, *sys.argv[1:4], maximum=4 * 1024 * 1024)
    config = download(client, *sys.argv[4:7], maximum=64 * 1024)
    json.loads(config)  # Reject malformed config before starting the training module.
    with TemporaryDirectory(prefix="vertex-bootstrap-") as directory:
        package = Path(directory) / "training.zip"
        package.write_bytes(code)
        path = Path(directory) / "config.json"
        path.write_bytes(config)
        sys.path.insert(0, str(package))  # No archive extraction or pip/network install.
        sys.argv = ["synthetic_quality_vertex.train", "--config", str(path)]
        runpy.run_module("synthetic_quality_vertex.train", run_name="__main__")


if __name__ == "__main__":
    main()
