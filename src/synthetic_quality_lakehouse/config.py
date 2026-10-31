"""Explicit immutable source and isolated output coordinates."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from importlib.resources import files
from urllib.parse import urlsplit

PROTECTED_BUCKET = "gcp-idq-data-984592102391"


def gcs_parts(uri):
    parsed = urlsplit(uri)
    if (
        parsed.scheme != "gs"
        or not parsed.netloc
        or not parsed.path.strip("/")
        or parsed.query
        or parsed.fragment
        or any(c in uri for c in "*?[]\\")
        or any(p in {"", ".", ".."} for p in parsed.path[1:].split("/"))
        or parsed.netloc == PROTECTED_BUCKET
    ):
        raise ValueError("Use an exact gs:// object outside the protected bucket")
    return parsed.netloc, parsed.path[1:]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def code_digest():
    """Works from an installed package or the Dataproc --py-files ZIP."""
    digest = hashlib.sha256()
    root = files("synthetic_quality_lakehouse")
    for name in sorted(p.name for p in root.iterdir() if p.name.endswith(".py")):
        digest.update(name.encode() + b"\0" + root.joinpath(name).read_bytes())
    for name in ("publish.sql", "reconcile.sql"):
        digest.update(root.joinpath("sql", name).read_bytes())
    digest.update(files("synthetic_quality").joinpath("schema.py").read_bytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class RunConfig:
    source_uri: str
    source_generation: int
    source_sha256: str
    output_uri: str
    run_id: str
    max_bytes: int = 10 * 1024 * 1024
    max_rows: int = 100_000

    def __post_init__(self):
        gcs_parts(self.source_uri)
        _, prefix = gcs_parts(self.output_uri)
        if not prefix.startswith("lakehouse/") or "/runs/" in prefix:
            raise ValueError("Outputs must use a dedicated lakehouse/ base prefix")
        if (
            not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", self.run_id)
            or type(self.source_generation) is not int
            or self.source_generation < 1
            or not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256)
            or type(self.max_bytes) is not int
            or not 1 <= self.max_bytes <= 10 * 1024 * 1024
            or type(self.max_rows) is not int
            or not 1 <= self.max_rows <= 100_000
        ):
            raise ValueError("Invalid run identity, source generation/hash or pilot size bounds")

    @property
    def root(self):
        return self.output_uri.rstrip("/") + "/runs/" + self.run_id

    def request(self, spark_version):
        return {
            "format_version": 1,
            "config": asdict(self),
            "code_sha256": code_digest(),
            "spark_version": spark_version,
        }


def identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value):
        raise ValueError("Invalid BigQuery dataset/table identifier")
    return value


def dataset_name(project, dataset):
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project):
        raise ValueError("Invalid BigQuery project ID")
    return project + "." + identifier(dataset)
