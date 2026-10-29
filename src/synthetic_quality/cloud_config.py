"""Validated, bounded configuration for the finite synthetic-data cloud job."""

import argparse
import os
import re
from dataclasses import dataclass

from synthetic_quality.pipeline import PipelineConfig


def validate_object_name(name: str) -> str:
    """Accept only explicit relative GCS keys, never URI/path normalization."""
    if not name or len(name.encode("utf-8")) > 1024:
        raise ValueError("GCS object name must contain 1..1024 bytes")
    if any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", part) or part in {".", ".."}
        for part in name.split("/")
    ):
        raise ValueError("Unsafe GCS object name: use relative, nonempty safe path segments")
    return name


@dataclass(frozen=True)
class CloudConfig:
    project_id: str
    bucket: str
    run_id: str
    prefix: str = "synthetic-quality"
    training_rows: int = 5_000
    evaluation_rows: int = 1_000
    training_seed: int = 42
    evaluation_seed: int = 123

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", self.project_id):
            raise ValueError("project_id is required and must be a valid GCP project ID")
        # Deliberately narrower than all legal GCS names: no domains, underscores or IPs.
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", self.bucket)
            or self.bucket.startswith("goog")
            or "google" in self.bucket
        ):
            raise ValueError("bucket is required: use 3..63 lowercase letters, digits or hyphens")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.run_id):
            raise ValueError(
                "run_id is required: use 1..64 letters, digits, underscores or hyphens"
            )
        if len(self.prefix) > 200 or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]*(/[A-Za-z0-9][A-Za-z0-9_-]*)*", self.prefix
        ):
            raise ValueError("prefix must be a safe relative path of at most 200 characters")
        for name in ("training_rows", "evaluation_rows"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 100_000:
                raise ValueError(f"{name} must be an integer in 1..100000 (Phase 1 safety cap)")
        for name in ("training_seed", "evaluation_seed"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 2**32 - 1:
                raise ValueError(f"{name} must be an integer in 0..4294967295")
        self.pipeline_config()  # Includes the independent-seeds invariant.

    def pipeline_config(self) -> PipelineConfig:
        return PipelineConfig(
            training_rows=self.training_rows,
            evaluation_rows=self.evaluation_rows,
            training_seed=self.training_seed,
            evaluation_seed=self.evaluation_seed,
        )

    def object_name(self, category: str, filename: str) -> str:
        return validate_object_name(f"{self.prefix}/{category}/{self.run_id}/{filename}")


def parse_config(argv: list[str] | None = None) -> CloudConfig:
    parser = argparse.ArgumentParser(
        description="Run the synthetic pipeline once and upload to GCS"
    )
    for name, env, default, kind in (
        ("project-id", "GCP_PROJECT_ID", "", str),
        ("bucket", "GCS_BUCKET", "", str),
        ("run-id", "RUN_ID", "", str),
        ("prefix", "GCS_PREFIX", "synthetic-quality", str),
        ("training-rows", "TRAINING_ROWS", "5000", int),
        ("evaluation-rows", "EVALUATION_ROWS", "1000", int),
        ("training-seed", "TRAINING_SEED", "42", int),
        ("evaluation-seed", "EVALUATION_SEED", "123", int),
    ):
        parser.add_argument(f"--{name}", default=os.environ.get(env, default), type=kind, help=env)
    try:
        return CloudConfig(**vars(parser.parse_args(argv)))
    except ValueError as error:
        parser.error(str(error))
