"""Frozen input references, model settings and reproducibility identity."""

import hashlib
import json
import platform
import re
from dataclasses import asdict, dataclass, field
from importlib.metadata import version
from importlib.resources import files

from synthetic_quality.model import ModelConfig
from synthetic_quality_lakehouse.config import canonical, gcs_parts

PROJECT = "gcp-intelligent-data-quality"
REGION = "asia-south1"
IMAGE = (
    "asia-south1-docker.pkg.dev/gcp-intelligent-data-quality/"
    "synthetic-quality-phase1/synthetic-quality@sha256:"
    "35a1e032472e06e4b21c55996b46faa0090deef3a4bdee710f27ddcb85e33116"
)


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


@dataclass(frozen=True)
class TrainingConfig:
    run_id: str
    training: ObjectRef
    evaluation: ObjectRef
    truth: ObjectRef
    output_uri: str = "gs://synthetic-quality-984592102391-data-as1/lakehouse/vertex"
    model: ModelConfig = field(default_factory=ModelConfig)
    baseline_quantile: float = 0.995
    max_rows: int = 10_000

    def __post_init__(self):
        _, path = gcs_parts(self.output_uri)
        if (
            not path.startswith("lakehouse/vertex")
            or "/runs/" in path
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", self.run_id)
            or not 0 < self.baseline_quantile < 1
            or type(self.max_rows) is not int
            or not 2 <= self.max_rows <= 10_000
            or self.training.uri == self.evaluation.uri
            or self.training.sha256 == self.evaluation.sha256
            or not 1 <= self.model.n_estimators <= 200
            or not 1 <= self.model.max_samples <= 256
            or type(self.model.random_state) is not int
            or not 0 <= self.model.random_state < 2**32
        ):
            raise ValueError("Invalid split identity, small-data bound or model configuration")

    @property
    def root(self):
        return self.output_uri.rstrip("/") + "/runs/" + self.run_id

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        for key in ("training", "evaluation", "truth"):
            value[key] = ObjectRef(**value[key])
        if "model" in value:
            value["model"] = ModelConfig(**value["model"])
        return cls(**value)

    def to_dict(self):
        return asdict(self)


def read_ref(store, ref, max_bytes=10 * 1024 * 1024):
    data, generation = store.read(ref.uri, ref.generation, max_bytes)
    if generation != ref.generation or hashlib.sha256(data).hexdigest() != ref.sha256:
        raise ValueError("Pinned input generation/hash differs")
    return data


def code_digest():
    digest = hashlib.sha256()
    for package in ("synthetic_quality", "synthetic_quality_lakehouse", "synthetic_quality_vertex"):
        root = files(package)
        for item in sorted(root.iterdir(), key=lambda p: p.name):
            if item.name.endswith(".py"):
                digest.update((package + "/" + item.name).encode() + b"\0" + item.read_bytes())
        if package == "synthetic_quality_lakehouse":
            for name in ("publish.sql", "reconcile.sql"):
                digest.update(root.joinpath("sql", name).read_bytes())
    return digest.hexdigest()


def request(config):
    return {
        "format_version": 1,
        "config": config.to_dict(),
        "code_sha256": code_digest(),
        "python": platform.python_version(),
        "packages": {
            p: version(p)
            for p in (
                "numpy",
                "pandas",
                "scikit-learn",
                "joblib",
                "python-dateutil",
                "google-cloud-storage",
            )
        },
    }


def decode_rows(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("Nonstandard JSON number")

    return [
        json.loads(line, object_pairs_hook=pairs, parse_constant=invalid)
        for line in data.decode("utf-8", errors="strict").splitlines()
    ]


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()
