import copy
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from conftest import SharedSession
from google.api_core.exceptions import Forbidden

from synthetic_quality_lakehouse.config import RunConfig, gcs_parts
from synthetic_quality_lakehouse.driver import run
from synthetic_quality_lakehouse.manifest import validate_manifest


def test_completed_replay_verifies_without_starting_spark(completed, source_config, objects):
    before = copy.deepcopy(objects.client.buckets["offline-data"].objects)
    forbidden = Mock(side_effect=AssertionError("Replay must not start Spark"))
    assert run(source_config, objects, forbidden) == completed
    assert objects.client.buckets["offline-data"].objects == before
    forbidden.assert_not_called()


def test_changed_input_identity_cannot_reuse_run(completed, source_config, objects):
    with pytest.raises(ValueError, match="conflict"):
        run(replace(source_config, source_generation=source_config.source_generation + 1), objects)


def test_partial_upload_recovery_preserves_completed_objects(source_config, objects, spark):
    bucket = objects.client.buckets["offline-data"]
    bucket.fail_before = gcs_parts(source_config.root + "/quarantine.ndjson")[1]
    with pytest.raises(RuntimeError, match="upload"):
        run(source_config, objects, lambda: SharedSession(spark))
    assert objects.optional(source_config.root + "/completion.json") is None
    clean = objects.read(source_config.root + "/clean.ndjson")
    result = run(source_config, objects, lambda: SharedSession(spark))
    assert result["status"] == "COMPLETE"
    assert objects.read(source_config.root + "/clean.ndjson") == clean


def test_lost_completion_ack_reconciles_without_recomputing(source_config, objects, spark):
    bucket = objects.client.buckets["offline-data"]
    bucket.lose_ack = gcs_parts(source_config.root + "/completion.json")[1]
    with pytest.raises(RuntimeError, match="acknowledgement"):
        run(source_config, objects, lambda: SharedSession(spark))
    assert run(source_config, objects, Mock(side_effect=AssertionError()))["status"] == "COMPLETE"


def test_changed_completed_artifact_is_not_silently_repaired(completed, source_config, objects):
    bucket = objects.client.buckets["offline-data"]
    key = gcs_parts(completed["artifacts"]["clean"]["uri"])[1]
    bucket.objects[key] = b"corrupted", 999
    with pytest.raises(ValueError, match="changed"):
        run(source_config, objects)


def test_read_permissions_are_not_missing(objects):
    objects.client.bucket = Mock(side_effect=Forbidden("fixture"))
    with pytest.raises(Forbidden):
        objects.optional("gs://offline-data/lakehouse/anything")


def test_source_generation_and_size_bounds(source_config, objects):
    with pytest.raises(FileNotFoundError):
        objects.read(source_config.source_uri, source_config.source_generation + 1)
    with pytest.raises(ValueError, match="bound"):
        objects.read(source_config.source_uri, max_bytes=1)


@pytest.mark.parametrize(
    "uri",
    [
        "gs://gcp-idq-data-984592102391/x",
        "gs://b/x#2",
        "gs://b/x*",
        "https://b/x",
        "gs://b/x/../z",
        "gs://b//x",
    ],
)
def test_unsafe_or_protected_paths_rejected(uri):
    with pytest.raises(ValueError):
        gcs_parts(uri)


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "../oops"),
        ("source_generation", 0),
        ("max_bytes", 100_000_000),
        ("source_sha256", "not-a-hash"),
        ("output_uri", "gs://offline-data/synthetic-quality"),
        ("max_rows", True),
    ],
)
def test_invalid_configuration_fails_before_io(source_config, field, value):
    with pytest.raises(ValueError):
        RunConfig(**{**source_config.__dict__, field: value})


def test_invalid_manifest_count_cannot_be_loaded(completed):
    manifest = copy.deepcopy(completed)
    manifest["source_rows"] += 1
    with pytest.raises(ValueError, match="reconcile"):
        validate_manifest(manifest)


def test_completion_json_is_not_published_before_all_artifacts(completed, source_config, objects):
    bucket = objects.client.buckets["offline-data"]
    keys = bucket.attempts
    assert keys[-1].endswith("completion.json")
    assert set(completed["artifacts"]) == {"clean", "quarantine", "quality", "ledger"}
    assert json.loads(objects.read(source_config.root + "/completion.json")[0]) == completed


def test_256_row_generated_pilot_has_exact_clean_quarantine_and_quality_counts(
    tmp_path, source_config, objects, spark
):
    import hashlib
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "tools/generate_lakehouse_source.py"
    spec = importlib.util.spec_from_file_location("fixture_generator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    local = tmp_path / "pilot.ndjson"
    module.generate(local, rows=256, invalid=True)
    record = objects.create("gs://offline-data/lakehouse/inputs/smoke.ndjson", local.read_bytes())
    config = replace(
        source_config,
        source_uri=record["uri"],
        source_generation=record["generation"],
        source_sha256=hashlib.sha256(local.read_bytes()).hexdigest(),
        run_id="smoke-256",
    )
    result = run(config, objects, lambda: SharedSession(spark))
    assert result["source_rows"] == 256
    assert {k: v["row_count"] for k, v in result["artifacts"].items()} == {
        "clean": 252,
        "quarantine": 4,
        "quality": 12,
        "ledger": 256,
    }
